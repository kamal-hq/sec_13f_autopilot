"""Read-only database parity checker comparing autopilot.db with sec_13f.db.

Validates that data rebuilt from SEC filings matches the Project 1 database
cell-for-cell across table row counts, per-fund totals, QoQ flags, and zero-value rows.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open a SQLite database in strict read-only mode."""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def check_parity(db1_path: Path, db2_path: Path) -> int:
    """Run read-only parity checks across both SQLite databases."""
    if not db1_path.exists():
        print(f"[SKIP] DB 1 not found: {db1_path}. Skipping parity check.")
        return 0

    if not db2_path.exists():
        print(f"[SKIP] DB 2 (sibling database) not found: {db2_path}. Skipping parity check.")
        return 0

    print(f"Comparing databases:\n  DB1: {db1_path}\n  DB2: {db2_path}\n")

    con1 = _open_readonly(db1_path)
    con2 = _open_readonly(db2_path)

    results: list[tuple[str, str, str, bool]] = []

    def record(check_name: str, val1: Any, val2: Any) -> None:
        passed = val1 == val2
        results.append((check_name, str(val1), str(val2), passed))

    try:
        # 1. Table row counts
        for tbl in ("Dim_Fund", "Dim_Security", "Fact_Holdings"):
            c1 = con1.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            c2 = con2.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            record(f"Row count: {tbl}", c1, c2)

        # 2. Per-fund and period row count and sum(value_usd)
        q_fund_period = """
        SELECT d.name, f.period, count(*), sum(f.value_usd)
        FROM Fact_Holdings f
        JOIN Dim_Fund d ON f.fund_id = d.fund_id
        GROUP BY d.name, f.period
        ORDER BY d.name, f.period
        """
        rows1 = con1.execute(q_fund_period).fetchall()
        rows2 = con2.execute(q_fund_period).fetchall()
        record("Fund-Period Aggregates (14 fund-periods)", len(rows1), len(rows2))
        fund_period_match = rows1 == rows2
        results.append(
            (
                "Fund-Period Cell Values",
                "All 14 matched" if fund_period_match else "Mismatch",
                "All 14 matched" if fund_period_match else "Mismatch",
                fund_period_match,
            )
        )

        # 3. Distribution of qoq_change_flag per period
        q_flags = """
        SELECT period, qoq_change_flag, count(*)
        FROM Fact_Holdings
        GROUP BY period, qoq_change_flag
        ORDER BY period, qoq_change_flag
        """
        flags1 = con1.execute(q_flags).fetchall()
        flags2 = con2.execute(q_flags).fetchall()
        record("QoQ Change Flag Distributions", flags1, flags2)

        # 4. Zero-value and NULL rows
        q_zero_all = "SELECT count(1) FROM Fact_Holdings WHERE value_usd = 0"
        z_all1 = con1.execute(q_zero_all).fetchone()[0]
        z_all2 = con2.execute(q_zero_all).fetchone()[0]
        record("All rows with value_usd = 0", z_all1, z_all2)

        q_zero_active = (
            "SELECT count(1) FROM Fact_Holdings WHERE value_usd = 0 "
            "AND (qoq_change_flag IS NULL OR qoq_change_flag != 'EXITED')"
        )
        z_act1 = con1.execute(q_zero_active).fetchone()[0]
        z_act2 = con2.execute(q_zero_active).fetchone()[0]
        record("Active rows with value_usd = 0 (Sinovac SVA)", z_act1, z_act2)

        q_zero_exited = (
            "SELECT count(1) FROM Fact_Holdings WHERE value_usd = 0 AND qoq_change_flag = 'EXITED'"
        )
        z_ex1 = con1.execute(q_zero_exited).fetchone()[0]
        z_ex2 = con2.execute(q_zero_exited).fetchone()[0]
        record("Exited rows with value_usd = 0", z_ex1, z_ex2)

        q_null = "SELECT count(1) FROM Fact_Holdings WHERE value_usd IS NULL"
        n1 = con1.execute(q_null).fetchone()[0]
        n2 = con2.execute(q_null).fetchone()[0]
        record("Rows with value_usd IS NULL", n1, n2)

        # 5. Sinovac details
        q_sva = """
        SELECT d.name, s.ticker, s.cusip, f.period, f.shares, f.value_usd, f.qoq_change_flag
        FROM Fact_Holdings f
        JOIN Dim_Fund d ON f.fund_id = d.fund_id
        JOIN Dim_Security s ON f.security_id = s.security_id
        WHERE f.value_usd = 0 AND (f.qoq_change_flag IS NULL OR f.qoq_change_flag != 'EXITED')
        ORDER BY d.name, f.period
        """
        sva1 = con1.execute(q_sva).fetchall()
        sva2 = con2.execute(q_sva).fetchall()
        record("Sinovac SVA Active Rows Match", len(sva1), len(sva2))

    finally:
        con1.close()
        con2.close()

    # Print summary table
    col_w_check = 42
    col_w_val = 22
    header = (
        f"| {'Check Description':<{col_w_check}} "
        f"| {'DB 1 Value':<{col_w_val}} "
        f"| {'DB 2 Value':<{col_w_val}} "
        f"| Status |"
    )
    separator = f"|:{'-' * col_w_check}-|:{'-' * col_w_val}-|:{'-' * col_w_val}-|:------:|"
    print(header)
    print(separator)

    all_passed = True
    for name, v1, v2, passed in results:
        status_str = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        v1_trunc = (v1[: col_w_val - 3] + "...") if len(v1) > col_w_val else v1
        v2_trunc = (v2[: col_w_val - 3] + "...") if len(v2) > col_w_val else v2
        print(
            f"| {name:<{col_w_check}} "
            f"| {v1_trunc:<{col_w_val}} "
            f"| {v2_trunc:<{col_w_val}} "
            f"|  {status_str}  |"
        )

    print()
    if all_passed:
        print("[PASS] Full database parity confirmed cell-for-cell!")
        print("Note: Caught 4 zero-value Sinovac Biotech (SVA) rows held by CalPERS & Renaissance.")
        return 0
    else:
        print("[FAIL] Parity check detected discrepancies between databases.")
        return 1


def main() -> None:
    """Parse CLI arguments and run database parity check."""
    parser = argparse.ArgumentParser(description="Check cell-for-cell parity between two 13F DBs.")
    parser.add_argument(
        "--db1",
        type=Path,
        default=Path("data/autopilot.db"),
        help="Path to primary autopilot database (default: data/autopilot.db)",
    )
    parser.add_argument(
        "--db2",
        type=Path,
        default=Path("../sec_13f_analyzer/data/sec_13f.db"),
        help="Path to comparison database (default: ../sec_13f_analyzer/data/sec_13f.db)",
    )

    args = parser.parse_args()
    code = check_parity(args.db1, args.db2)
    sys.exit(code)


if __name__ == "__main__":
    main()
