"""DetectStage stub: polls SEC EDGAR submissions for new quarterly Form 13F-HR/A filings.

Stage Contract
--------------
- **Stage Name**: ``detect``
- **Input Contract**:
  - ``context.target_period`` (``str``, e.g., ``"2026-Q2"``): Target reporting quarter.
  - ``settings.funds`` (``dict[str, str]``): Mapping of institutional fund names to 10-digit CIKs.
  - ``settings.state_dir`` (``Path``): Directory storing previously ingested accession state.
  - ``SecGateway``: Rate-limited SEC EDGAR gateway (<= 8 req/s) used to call
    ``get_latest_13f_metadata(cik)``.
- **Output Contract**:
  - Writes ``context.detection`` as a :class:`autopilot.models.DetectionResult` containing:
    - ``period`` (``str``): Target reporting period.
    - ``new_filings`` (``list[SecFilingMetadata]``): Filings matching ``target_period`` not yet
      recorded in local state.
    - ``already_ingested`` (``list[str]``): Accession numbers already processed.
    - ``has_updates`` (``bool``): ``True`` if at least one new or restated filing is ready.
- **Side Effects & Invariants**:
  - Performs read-only network calls exclusively through ``SecGateway``.
  - Does not mutate the SQLite database.
"""

from __future__ import annotations

from autopilot.models import PipelineContext, StageStatus
from autopilot.stages.base import Stage, StageResult


class DetectStage(Stage):
    """Stub stage that checks SEC EDGAR submissions for new 13F filings."""

    name: str = "detect"

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute filing detection (stub — see module docstring for contract)."""
        _ = context
        raise NotImplementedError(
            "DetectStage is a scaffold stub; see docstring for input/output contract."
        )
