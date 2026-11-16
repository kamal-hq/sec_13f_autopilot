"""ReviewStage: enforces the mandatory human review gate via Pull Request before publishing.

Stage Contract
--------------
- **Stage Name**: ``review``
- **Input Contract**:
  - ``context.validation`` (:class:`autopilot.models.ValidationResult`): Must have
    ``passed == True`` (or ``reports/<period>/validation_report.json`` has ``status == "PASS"``).
  - ``context.draft`` (:class:`autopilot.models.NarrativeDraft`): Validated markdown note.
  - ``context.facts`` (:class:`autopilot.models.FactsBundle`): Accompanying quantitative audit
    trail written to ``reports/<period>/``.
- **Output Contract**:
  - Writes ``context.review`` as a :class:`autopilot.models.ReviewDecision` containing:
    - ``approved`` (``bool``): ``True`` when validation passes and artifacts are staged for the PR
      gate or state commit.
    - ``pr_number`` (``int | None``): Associated pull request identifier, if opened.
    - ``reviewer`` (``str | None``): Identifier of the review gate state.
    - ``notes`` (``str``): Review summary or instructions.
  - Returns ``StageStatus.SUCCEEDED`` when validation passes so ``PublishStage`` can stage outputs
    for the Pull Request review gate.
- **Side Effects & Invariants**:
  - Enforces fail-closed validation: unvalidated or failed drafts cannot proceed to publish.
"""

from __future__ import annotations

import json
import logging

from autopilot.models import PipelineContext, ReviewDecision, StageStatus
from autopilot.settings import Settings
from autopilot.stages.base import Stage, StageError, StageResult

logger = logging.getLogger(__name__)


class ReviewStage(Stage):
    """Enforces validation check before staging outputs for the human Pull Request review gate."""

    name: str = "review"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def run(self, context: PipelineContext) -> StageResult:
        """Verify validation passed and record ReviewDecision for PublishStage."""
        period = context.target_period
        passed = False
        if context.validation is not None:
            passed = bool(context.validation.passed)
        else:
            val_path = self.settings.reports_dir / period / "validation_report.json"
            if val_path.exists():
                try:
                    val_data = json.loads(val_path.read_text(encoding="utf-8"))
                    passed = val_data.get("status") == "PASS"
                except Exception:
                    passed = False

        if not passed:
            raise StageError(
                f"ReviewStage blocked: validation_report for period {period} did not PASS."
            )

        decision = ReviewDecision(
            approved=True,
            reviewer="commit-state" if context.commit_state else "pr-review-gate",
            notes=(
                "Validation passed; committing state (--commit-state)"
                if context.commit_state and not context.replay
                else "Validation passed; staging artifacts for PR review gate (state untouched)"
            ),
        )
        context.review = decision
        context.artifacts["review_decision"] = decision
        logger.info(
            "ReviewStage verified validation PASS for %s (commit_state=%s, replay=%s)",
            period,
            context.commit_state,
            context.replay,
        )
        return StageResult(
            status=StageStatus.SUCCEEDED,
            message=decision.notes,
            metadata={
                "approved": decision.approved,
                "commit_state": context.commit_state,
                "replay": context.replay,
            },
        )
