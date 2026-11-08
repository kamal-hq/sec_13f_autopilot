"""ReviewStage stub: enforces the mandatory human review gate via Pull Request before publishing.

Stage Contract
--------------
- **Stage Name**: ``review``
- **Input Contract**:
  - ``context.validation`` (:class:`autopilot.models.ValidationResult`): Must have
    ``passed == True``.
  - ``context.draft`` (:class:`autopilot.models.NarrativeDraft`): Validated markdown note.
  - ``context.facts`` (:class:`autopilot.models.FactsBundle`): Accompanying quantitative audit
    trail written to ``reports/<period>/``.
- **Output Contract**:
  - Writes ``context.review`` as a :class:`autopilot.models.ReviewDecision` containing:
    - ``approved`` (``bool``): ``True`` only when a human reviewer has explicitly approved the
      staged report artifact / PR.
    - ``pr_number`` (``int | None``): Associated pull request identifier, if opened.
    - ``reviewer`` (``str | None``): Identifier of the approving human reviewer.
    - ``notes`` (``str``): Review summary or instructions.
  - Returns ``StageStatus.AWAITING_REVIEW`` when staged for human sign-off, or
    ``StageStatus.SUCCEEDED`` once approval is recorded.
- **Side Effects & Invariants**:
  - Enforces the human-in-the-loop governance gate: unapproved drafts are never auto-published.
"""

from __future__ import annotations

from autopilot.models import PipelineContext, StageStatus
from autopilot.stages.base import Stage, StageResult


class ReviewStage(Stage):
    """Stub stage that manages the human Pull Request review gate."""

    name: str = "review"

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute human review gate check (stub — see module docstring for contract)."""
        _ = context
        raise NotImplementedError(
            "ReviewStage is a scaffold stub; see docstring for input/output contract."
        )
