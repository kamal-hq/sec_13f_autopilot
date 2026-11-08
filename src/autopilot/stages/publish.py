"""PublishStage stub: writes finalized reports, state checkpoints, and run metrics after approval.

Stage Contract
--------------
- **Stage Name**: ``publish``
- **Input Contract**:
  - ``context.review`` (:class:`autopilot.models.ReviewDecision`): Must have
    ``approved == True``.
  - ``context.draft`` (:class:`autopilot.models.NarrativeDraft`): Approved research note.
  - ``context.facts`` (:class:`autopilot.models.FactsBundle`): Verified quantitative bundle.
  - ``settings.reports_dir`` (``Path``) and ``settings.state_dir`` (``Path``).
- **Output Contract**:
  - Writes ``context.publish`` as a :class:`autopilot.models.PublishResult` containing:
    - ``period`` (``str``): Published reporting quarter.
    - ``published_files`` (``list[str]``): Paths of finalized report and metrics artifacts
      (e.g., ``reports/<period>/report.md``, ``reports/<period>/facts.json``,
      ``state/ingested_filings.json``).
    - ``commit_sha`` (``str | None``): Git commit SHA if committed in CI.
- **Side Effects & Invariants**:
  - Refuses to execute if ``context.review`` is missing or ``approved is False``.
"""

from __future__ import annotations

from autopilot.models import PipelineContext, StageStatus
from autopilot.stages.base import Stage, StageResult


class PublishStage(Stage):
    """Stub stage that publishes human-approved 13F reports and updates state."""

    name: str = "publish"

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute report publication (stub — see module docstring for contract)."""
        _ = context
        raise NotImplementedError(
            "PublishStage is a scaffold stub; see docstring for input/output contract."
        )
