"""Persistent state management for `state/processed_filings.json` with atomic writes.

Invariants:
- Schema includes `schema_version` and maps `accession -> {period, filed_date, ...}`.
- Writes use temp-file + `os.fsync` + `os.replace` so partial writes never corrupt state.
- `detect` and `ingest` stages NEVER write state (state updates occur only in `publish`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 1


def _utc_now_iso() -> str:
    """Return current UTC timestamp formatted as ISO-8601."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ProcessedFilingRecord:
    """State record for a single processed 13F filing accession."""

    accession: str
    period: str
    filed_date: str
    processed_at: str
    supersedes: str | list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize record for `state/processed_filings.json`."""
        return {
            "period": self.period,
            "filed_date": self.filed_date,
            "processed_at": self.processed_at,
            "supersedes": self.supersedes,
        }


class StateStore:
    """Manages reading and atomic writing of `state/processed_filings.json`."""

    def __init__(self, path: Path | str = Path("state/processed_filings.json")) -> None:
        self.path = Path(path)
        self.schema_version: int = SCHEMA_VERSION
        self._records: dict[str, ProcessedFilingRecord] = {}
        self.load()

    @property
    def records(self) -> dict[str, ProcessedFilingRecord]:
        """Return a shallow copy of processed filing records keyed by accession."""
        return dict(self._records)

    def load(self) -> dict[str, ProcessedFilingRecord]:
        """Load processed filings from disk if `self.path` exists."""
        self._records.clear()
        if not self.path.exists():
            return {}

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Expected JSON object in {self.path}, got {type(raw).__name__}")

        self.schema_version = int(raw.get("schema_version", SCHEMA_VERSION))
        filings_map = raw.get("filings")
        if not isinstance(filings_map, dict):
            filings_map = {
                k: v for k, v in raw.items() if k != "schema_version" and isinstance(v, dict)
            }

        for acc, entry in filings_map.items():
            if not isinstance(entry, dict):
                continue
            self._records[str(acc)] = ProcessedFilingRecord(
                accession=str(acc),
                period=str(entry.get("period", "")),
                filed_date=str(entry.get("filed_date", "")),
                processed_at=str(entry.get("processed_at", "")),
                supersedes=entry.get("supersedes"),
            )
        return dict(self._records)

    def processed_accessions(self, *, as_of: str | None = None) -> set[str]:
        """Return processed accession numbers (filtered by `filed_date <= as_of` if given)."""
        if as_of is None:
            return set(self._records.keys())
        return {
            acc
            for acc, rec in self._records.items()
            if not rec.filed_date or rec.filed_date <= as_of
        }

    def is_processed(self, accession: str, *, as_of: str | None = None) -> bool:
        """Return True if `accession` has been processed (respecting optional `as_of` date)."""
        return accession in self.processed_accessions(as_of=as_of)

    def record_filing(
        self,
        accession: str,
        *,
        period: str,
        filed_date: str,
        processed_at: str | None = None,
        supersedes: str | list[str] | None = None,
    ) -> ProcessedFilingRecord:
        """Stage a processed filing record in memory without writing to disk."""
        rec = ProcessedFilingRecord(
            accession=accession,
            period=period,
            filed_date=filed_date,
            processed_at=processed_at or _utc_now_iso(),
            supersedes=supersedes,
        )
        self._records[accession] = rec
        return rec

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable state payload."""
        return {
            "schema_version": self.schema_version,
            "filings": {acc: rec.to_dict() for acc, rec in sorted(self._records.items())},
        }

    def save(self, *, dry_run: bool = False) -> None:
        """Atomically persist state to `self.path` using temp-file + fsync + os.replace."""
        if dry_run:
            logger.info("Dry-run: skipping state write to %s", self.path)
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload_text = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(payload_text)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            os.replace(tmp_path, self.path)
            logger.info(
                "Atomically wrote %d processed filings to %s",
                len(self._records),
                self.path,
            )
        except Exception:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
