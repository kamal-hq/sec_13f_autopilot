"""IngestStage: loads target and previous periods into a fresh SQLite DB and reconciles totals.

Stage Contract
--------------
- **Stage Name**: ``ingest``
- **Input Contract**:
  - ``context.target_period`` (``str``, e.g., ``"2026-Q2"``): Period to process.
  - ``context.detection`` (:class:`autopilot.models.DetectResult` | ``None``): Active filings
    discovered by ``DetectStage`` (auto-discovered via ``DetectStage`` if ``None``).
  - ``settings.data_dir`` (``Path``) and ``settings.db_path`` (``Path``, default
    ``data/autopilot.db``).
  - ``sec_13f_analyzer`` library: Imported exclusively via ``from sec_13f_analyzer import ...``
    with explicit paths (`raw_dir`, `db_path`, `cusip_cache_path`, `sector_cache_path`,
    `split_cache_path`, `filing_periods`, `target_funds`). Never calls ``configure()``.
- **Output Contract**:
  - Writes ``context.ingest`` as an :class:`autopilot.models.IngestResult` containing table
    and period row counts, cover-page ``<tableValueTotal>`` reconciliations, detected splits
    (confirmed vs. unconfirmed), and amendments applied.
  - Writes ``reports/<period>/ingest.json`` (unless ``context.dry_run`` is ``True``).
  - Never writes to ``state/processed_filings.json``.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

from sec_13f_analyzer import (
    COVER_PAGE_TOTALS,
    bulk_insert_holdings,
    create_schema,
    detect_splits,
    get_connection,
    map_cusips,
    map_sectors,
    parse_all_raw_files,
    parse_xml_file,
    validate_parsed_data,
)

from autopilot.gateways.sec import SecGateway
from autopilot.models import (
    AmendmentApplied,
    DetectedSplit,
    Filing,
    FundReconciliation,
    IngestResult,
    PeriodStatus,
    PipelineContext,
    StageStatus,
)
from autopilot.policies import compute_lookback_periods, compute_previous_period
from autopilot.settings import Settings
from autopilot.stages.base import Stage, StageError, StageResult
from autopilot.stages.detect import DetectStage, _resolve_sec_gateway
from autopilot.state import StateStore

logger = logging.getLogger(__name__)


class IngestStage(Stage):
    """Ingests target and previous periods into a fresh SQLite DB (`data/autopilot.db`)."""

    name: str = "ingest"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        sec_gateway: SecGateway | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self._settings = settings or Settings()
        self._sec_gateway = sec_gateway
        self._state_store = state_store

    def _resolve_period_filings(
        self,
        target_period: str,
        lookback_periods: list[str],
        *,
        as_of: str | None = None,
        existing_detection: object | None = None,
    ) -> tuple[list[str], list[Filing], list[Filing]]:
        """Resolve active filings for available `lookback_periods` and `target_period`."""
        detect_res = existing_detection
        earliest_period = lookback_periods[0] if lookback_periods else target_period
        if detect_res is None:
            detector = DetectStage(
                settings=self._settings,
                sec_gateway=self._sec_gateway,
                state_store=self._state_store,
                min_period=earliest_period,
            )
            detect_res = detector.detect(
                target_period=target_period,
                as_of=as_of,
                dry_run=True,
                write_reports=False,
            )

        periods_dict = getattr(detect_res, "periods", {})
        target_obj = periods_dict.get(target_period)
        if target_obj is not None and target_obj.status == PeriodStatus.NEEDS_REVIEW:
            raise StageError(
                f"Cannot auto-ingest period {target_period} with status NEEDS_REVIEW: "
                f"{target_obj.reason}"
            )

        loaded_periods: list[str] = []
        prior_filings: list[Filing] = []
        for lb_period in lookback_periods:
            lb_obj = periods_dict.get(lb_period)
            if lb_obj is None:
                continue
            if lb_obj.status == PeriodStatus.NEEDS_REVIEW:
                raise StageError(
                    f"Cannot auto-ingest baseline period {lb_period} with status NEEDS_REVIEW: "
                    f"{lb_obj.reason}"
                )
            if lb_obj.filings:
                loaded_periods.append(lb_period)
                prior_filings.extend(lb_obj.filings)

        target_filings = list(target_obj.filings) if target_obj is not None else []
        if not target_filings:
            raise StageError(f"No filings found to ingest for period {target_period}.")
        loaded_periods.append(target_period)
        return loaded_periods, prior_filings, target_filings

    def ingest(
        self,
        target_period: str | None = None,
        *,
        as_of: str | None = None,
        dry_run: bool = False,
        existing_detection: object | None = None,
        write_report: bool = True,
    ) -> IngestResult:
        """Load `target_period` and lookback periods into `data/autopilot.db`."""
        resolved_period = target_period or self._settings.target_period
        previous_period = compute_previous_period(resolved_period)
        lookback_list = compute_lookback_periods(
            resolved_period,
            self._settings.lookback_periods,
        )
        loaded_periods, prev_filings, curr_filings = self._resolve_period_filings(
            resolved_period,
            lookback_list,
            as_of=as_of,
            existing_detection=existing_detection,
        )
        active_filings = [*prev_filings, *curr_filings]

        db_path = self._settings.db_path
        if dry_run:
            logger.info(
                "Dry-run: skipping DB load and ingest.json write for %s (periods=%s)",
                resolved_period,
                loaded_periods,
            )
            return IngestResult(
                period=resolved_period,
                previous_period=previous_period,
                loaded_periods=loaded_periods,
                db_path=str(db_path),
                ingested_accessions=[f.accession for f in active_filings],
            )

        raw_store_dir = self._settings.data_dir / "raw"
        mappings_dir = self._settings.data_dir / "mappings"
        raw_store_dir.mkdir(parents=True, exist_ok=True)
        mappings_dir.mkdir(parents=True, exist_ok=True)

        cusip_cache_path = mappings_dir / "cusip_cache.json"
        sector_cache_path = mappings_dir / "sector_cache.json"
        split_cache_path = mappings_dir / "split_cache.json"

        gateway: SecGateway | None = None
        amendments_applied: list[AmendmentApplied] = []
        raw_row_counts: dict[str, int] = {}

        # Stage active XML files for (previous_period, target_period) in an isolated temp dir
        with tempfile.TemporaryDirectory(prefix="autopilot_raw_") as staged_raw_str:
            staged_raw_dir = Path(staged_raw_str)

            for filing in active_filings:
                padded_cik = filing.cik.strip().zfill(10)
                # Remove superseded XMLs from persistent raw_store_dir if present
                for sup_acc in filing.supersedes:
                    sup_path = raw_store_dir / f"{padded_cik}_{sup_acc}.xml"
                    if sup_path.exists():
                        sup_path.unlink(missing_ok=True)
                        logger.info("Removed superseded raw XML file %s", sup_path)

                if filing.form_type == "13F-HR/A" and filing.amendment_type == "RESTATEMENT":
                    amendments_applied.append(
                        AmendmentApplied(
                            cik=padded_cik,
                            fund_name=filing.fund_name,
                            period=filing.period,
                            accession=filing.accession,
                            amendment_type=filing.amendment_type,
                            filed_date=filing.filed_date,
                            supersedes=filing.supersedes,
                        )
                    )

                xml_filename = f"{padded_cik}_{filing.accession}.xml"
                local_xml_path = raw_store_dir / xml_filename
                if not local_xml_path.exists():
                    if gateway is None:
                        gateway = _resolve_sec_gateway(self._settings, self._sec_gateway)
                    xml_text = gateway.fetch_information_table_xml(
                        padded_cik,
                        filing.accession,
                        filing.infotable_document or None,
                    )
                    local_xml_path.write_text(xml_text, encoding="utf-8")

                staged_xml_path = staged_raw_dir / xml_filename
                shutil.copy2(local_xml_path, staged_xml_path)
                raw_row_counts[filing.accession] = len(parse_xml_file(str(staged_xml_path)))

            filing_periods_map = {f.accession: f.period for f in active_filings}
            df = parse_all_raw_files(
                raw_dir=str(staged_raw_dir),
                filing_periods=filing_periods_map,
                target_funds=self._settings.funds,
            )
            df = validate_parsed_data(df)

        unique_cusips = df["cusip"].unique().tolist()
        cusip_map = map_cusips(
            unique_cusips,
            cache_path=str(cusip_cache_path),
            api_key=self._settings.openfigi_api_key,
        )
        df["ticker"] = df["cusip"].map(lambda c: cusip_map.get(c, {}).get("ticker"))
        df["company_name"] = df.apply(
            lambda r: cusip_map.get(r["cusip"], {}).get("name") or r["issuer_name"],
            axis=1,
        )
        df["security_class"] = df["title_of_class"]

        unique_tickers = [t for t in df["ticker"].dropna().unique().tolist() if t]
        sector_map = map_sectors(
            unique_tickers,
            fetch_uncached=False,
            cache_path=str(sector_cache_path),
            user_agent=self._settings.sec_user_agent or None,
        )
        df["sector"] = df["ticker"].map(
            lambda t: (
                sector_map.get(str(t).upper(), "Unknown")
                if t is not None and str(t).strip() and str(t) != "None"
                else "Unknown"
            )
        )

        splits_df = detect_splits(df, split_cache_path=str(split_cache_path))
        detected_splits: list[DetectedSplit] = []
        confirmed_splits: list[DetectedSplit] = []
        unconfirmed_splits: list[DetectedSplit] = []

        if not splits_df.empty:
            for row in splits_df.to_dict("records"):
                status_str = str(row.get("confirmation_status", "UNCONFIRMED"))
                is_confirmed = status_str == "CONFIRMED"
                split_rec = DetectedSplit(
                    cusip=str(row["cusip"]),
                    ticker=str(row["ticker"]) if row.get("ticker") else None,
                    company_name=str(row["company_name"]) if row.get("company_name") else None,
                    split_factor=float(row["split_factor"]),
                    split_type=str(row["split_type"]),
                    detection_source=str(row["detection_source"]),
                    confirmation_status=status_str,
                    split_date=str(row["split_date"]) if row.get("split_date") else None,
                    applied=is_confirmed,
                )
                detected_splits.append(split_rec)
                if is_confirmed:
                    confirmed_splits.append(split_rec)
                else:
                    unconfirmed_splits.append(split_rec)

        # Fresh database at data/autopilot.db
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()

        create_schema(db_path=str(db_path))
        bulk_insert_holdings(
            df,
            db_path=str(db_path),
            split_cache_path=str(split_cache_path),
        )

        reconciliations: list[FundReconciliation] = []
        row_counts: dict[str, int] = {}

        with get_connection(db_path=str(db_path)) as conn:
            for tbl in ("Dim_Fund", "Dim_Security", "Fact_Holdings"):
                cnt = conn.execute(f"SELECT COUNT(*) AS cnt FROM {tbl}").fetchone()["cnt"]
                row_counts[tbl] = int(cnt)

            for p_name in loaded_periods:
                active_cnt = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM Fact_Holdings "
                    "WHERE period = ? AND COALESCE(qoq_change_flag, '') != 'EXITED'",
                    (p_name,),
                ).fetchone()["cnt"]
                exited_cnt = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM Fact_Holdings "
                    "WHERE period = ? AND qoq_change_flag = 'EXITED'",
                    (p_name,),
                ).fetchone()["cnt"]
                row_counts[f"active_holdings_{p_name}"] = int(active_cnt)
                row_counts[f"exited_holdings_{p_name}"] = int(exited_cnt)

            for filing in active_filings:
                padded_cik = filing.cik.strip().zfill(10)
                agg_row = conn.execute(
                    """
                    SELECT
                        COUNT(
                            CASE WHEN COALESCE(h.qoq_change_flag, '') != 'EXITED' THEN 1 END
                        ) AS agg_rows,
                        COALESCE(SUM(h.value_usd), 0) AS db_sum
                    FROM Fact_Holdings h
                    JOIN Dim_Fund f ON h.fund_id = f.fund_id
                    WHERE f.cik = ? AND h.period = ?
                    """,
                    (padded_cik, filing.period),
                ).fetchone()

                db_sum = int(agg_row["db_sum"])
                agg_rows = int(agg_row["agg_rows"])
                cover_total = (
                    int(filing.table_value_total)
                    if filing.table_value_total is not None
                    else int(COVER_PAGE_TOTALS.get(filing.accession, db_sum))
                )
                diff_usd = db_sum - cover_total
                pct_diff = (abs(diff_usd) / cover_total * 100.0) if cover_total > 0 else 0.0
                reconciliations.append(
                    FundReconciliation(
                        cik=padded_cik,
                        fund_name=filing.fund_name,
                        period=filing.period,
                        accession=filing.accession,
                        form_type=filing.form_type,
                        amendment_type=filing.amendment_type,
                        raw_rows=raw_row_counts.get(filing.accession, agg_rows),
                        aggregated_rows=agg_rows,
                        db_total_value_usd=db_sum,
                        cover_page_total_usd=cover_total,
                        diff_usd=diff_usd,
                        pct_diff=round(pct_diff, 6),
                        reconciled=(diff_usd == 0),
                    )
                )

        all_reconciled = all(r.reconciled for r in reconciliations)
        result = IngestResult(
            period=resolved_period,
            previous_period=previous_period,
            loaded_periods=loaded_periods,
            db_path=str(db_path),
            ingested_accessions=[f.accession for f in active_filings],
            holdings_row_count=row_counts.get("Fact_Holdings", 0),
            row_counts=row_counts,
            reconciliations=reconciliations,
            detected_splits=detected_splits,
            confirmed_splits=confirmed_splits,
            unconfirmed_splits=unconfirmed_splits,
            amendments_applied=amendments_applied,
            all_reconciled=all_reconciled,
        )

        if write_report:
            report_dir = self._settings.reports_dir / resolved_period
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / "ingest.json"
            result.report_path = str(report_path)
            report_path.write_text(
                json.dumps(result.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
            logger.info("Wrote ingest report to %s", report_path)

        return result

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute ingestion for `context.target_period` and populate `context.ingest`."""
        ingest_result = self.ingest(
            target_period=context.target_period,
            as_of=context.as_of,
            dry_run=context.dry_run,
            existing_detection=context.detection,
            write_report=not context.dry_run,
        )
        context.ingest = ingest_result
        return StageResult(
            status=StageStatus.SUCCEEDED,
            message=(
                f"period={ingest_result.period} prev={ingest_result.previous_period} "
                f"rows={ingest_result.holdings_row_count} "
                f"all_reconciled={ingest_result.all_reconciled}"
            ),
            metadata={
                "period": ingest_result.period,
                "previous_period": ingest_result.previous_period,
                "holdings_row_count": ingest_result.holdings_row_count,
                "all_reconciled": ingest_result.all_reconciled,
                "confirmed_splits": len(ingest_result.confirmed_splits),
                "unconfirmed_splits": len(ingest_result.unconfirmed_splits),
            },
        )
