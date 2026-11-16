"""Regenerate tracked `seed/` mapping caches from `data/mappings/` after secret verification.

Usage:
    python scripts/refresh_seed.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autopilot.settings import REDACTED_MASK, redact_text

SEED_FILES: tuple[str, ...] = (
    "cusip_cache.json",
    "sector_cache.json",
    "split_cache.json",
    "filing_metadata.json",
)


def refresh_seed_caches(
    source_dir: Path = Path("data/mappings"),
    seed_dir: Path = Path("seed"),
) -> dict[str, int]:
    """Copy public mapping caches from `source_dir` into `seed_dir` with secret verification."""
    seed_dir.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}

    for fname in SEED_FILES:
        src_path = source_dir / fname
        if not src_path.exists():
            raise FileNotFoundError(f"Required mapping cache not found: {src_path}")

        raw_text = src_path.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)
        normalized = json.dumps(parsed, indent=2, sort_keys=True) + "\n"

        redacted = redact_text(normalized)
        if REDACTED_MASK in redacted or normalized != redacted:
            raise ValueError(f"Secret redaction triggered on {fname}; refusing to write seed.")

        dest_path = seed_dir / fname
        dest_path.write_text(normalized, encoding="utf-8")
        sizes[fname] = dest_path.stat().st_size

    return sizes


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for regenerating `seed/` caches."""
    parser = argparse.ArgumentParser(
        description="Refresh tracked seed/ caches from data/mappings/."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("data/mappings"),
        help="Source mappings directory (default: data/mappings).",
    )
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=Path("seed"),
        help="Destination seed directory (default: seed).",
    )
    args = parser.parse_args(argv)

    sizes = refresh_seed_caches(source_dir=args.source_dir, seed_dir=args.seed_dir)
    print("| Seed Cache File | Size (Bytes) | Size (KB) | Secret Check |")
    print("| :--- | ---: | ---: | :---: |")
    for fname, byte_size in sizes.items():
        kb = byte_size / 1024.0
        print(f"| `seed/{fname}` | {byte_size:,} | {kb:.2f} KB | PASS (0 secrets) |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
