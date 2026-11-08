"""ValidateStage stub: verifies every number and section in `NarrativeDraft` against `FactsBundle`.

Stage Contract
--------------
- **Stage Name**: ``validate``
- **Input Contract**:
  - ``context.facts`` (:class:`autopilot.models.FactsBundle`): Contains ``allowed_numbers`` and
    canonical issuer/fund identifiers.
  - ``context.draft`` (:class:`autopilot.models.NarrativeDraft`): Candidate markdown note.
  - ``settings.max_validation_retries`` (``int``, default ``3``): Maximum narrative regeneration
    attempts before failing the pipeline.
- **Output Contract**:
  - Writes ``context.validation`` as a :class:`autopilot.models.ValidationResult` containing:
    - ``passed`` (``bool``): ``True`` iff zero hallucinated numbers and all required sections
      (including the non-advisory disclaimer) are present.
    - ``attempt`` (``int``): Validation attempt count (1..3).
    - ``unverified_numbers`` (``list[str]``): Numeric tokens in ``draft.markdown`` absent from
      ``facts.allowed_numbers``.
    - ``missing_sections`` (``list[str]``): Required headings or disclaimers missing from draft.
    - ``errors`` (``list[str]``): Human-readable validation failure reasons.
- **Side Effects & Invariants**:
  - Offline, deterministic verification; zero network calls.
  - Can trigger up to 3 retry cycles with ``NarrateStage`` while staying within ``BudgetGuard``.
"""

from __future__ import annotations

from autopilot.models import PipelineContext, StageStatus
from autopilot.stages.base import Stage, StageResult


class ValidateStage(Stage):
    """Stub stage that validates `NarrativeDraft` numbers against `FactsBundle`."""

    name: str = "validate"

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute narrative fact validation (stub — see module docstring for contract)."""
        _ = context
        raise NotImplementedError(
            "ValidateStage is a scaffold stub; see docstring for input/output contract."
        )
