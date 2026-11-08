"""IngestStage stub: downloads, parses, and loads Form 13F XML filings into SQLite.

Stage Contract
--------------
- **Stage Name**: ``ingest``
- **Input Contract**:
  - ``context.detection`` (:class:`autopilot.models.DetectionResult`): Must be populated with
    ``new_filings`` (`list[SecFilingMetadata]`) discovered by ``DetectStage``.
  - ``settings.data_dir`` (``Path``): Target data directory for XML archives and SQLite DB.
  - ``SecGateway``: Downloads primary cover pages and information table XML documents.
  - ``sec_13f_analyzer`` library: Invokes ``parse_holdings`` and ``database`` loaders to
    populate ``Dim_Fund``, ``Dim_Security``, and ``Fact_Holdings`` with split adjustments.
- **Output Contract**:
  - Writes ``context.ingest`` as an :class:`autopilot.models.IngestResult` containing:
    - ``period`` (``str``): Reporting period ingested.
    - ``ingested_accessions`` (``list[str]``): Accession numbers committed to SQLite.
    - ``db_path`` (``str``): Path to the updated SQLite star-schema database.
    - ``holdings_row_count`` (``int``): Number of ``Fact_Holdings`` rows inserted/updated.
- **Side Effects & Invariants**:
  - Idempotent on ``(fund_id, security_id, period, put_call)``.
  - Verifies cover-page ``<tableValueTotal>`` reconciliation tolerances before committing.
"""

from __future__ import annotations

from autopilot.models import PipelineContext, StageStatus
from autopilot.stages.base import Stage, StageResult


class IngestStage(Stage):
    """Stub stage that ingests new 13F filings via `sec_13f_analyzer`."""

    name: str = "ingest"

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute filing ingestion (stub — see module docstring for contract)."""
        _ = context
        raise NotImplementedError(
            "IngestStage is a scaffold stub; see docstring for input/output contract."
        )
