"""NarrateStage stub: renders prompts from `FactsBundle` and generates a research note draft.

Stage Contract
--------------
- **Stage Name**: ``narrate``
- **Input Contract**:
  - ``context.facts`` (:class:`autopilot.models.FactsBundle`): Verified quantitative dataset.
  - ``settings.prompts_dir`` (``Path``): Contains ``system.md``, ``note_template.md``, and
    ``style_guide.md``.
  - ``LLMProvider`` wrapped in :class:`autopilot.gateways.llm.BudgetGuard`: Selected via
    ``select_llm_provider(settings, wrap_with_budget_guard=True)``.
  - Optional prior ``context.validation`` errors if re-invoked during the validation retry loop.
- **Output Contract**:
  - Writes ``context.draft`` as a :class:`autopilot.models.NarrativeDraft` containing:
    - ``period`` (``str``): Target reporting period.
    - ``markdown`` (``str``): Drafted research note including the mandatory disclaimer.
    - ``llm_result`` (:class:`autopilot.models.LLMResult`): Provider metadata and token counts.
    - ``attempt`` (``int``): 1-based generation attempt index (up to ``max_validation_retries``).
- **Side Effects & Invariants**:
  - Never logs prompt or response bodies; logs only status and token counts.
  - Respects ``BudgetGuard`` limits (`max_calls_per_run=5`, `max_output_tokens`, `timeout`,
    and ``AUTOPILOT_LLM_DISABLED=1``).
"""

from __future__ import annotations

from autopilot.models import PipelineContext, StageStatus
from autopilot.stages.base import Stage, StageResult


class NarrateStage(Stage):
    """Stub stage that drafts the quarterly research note from `FactsBundle`."""

    name: str = "narrate"

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute narrative generation (stub — see module docstring for contract)."""
        _ = context
        raise NotImplementedError(
            "NarrateStage is a scaffold stub; see docstring for input/output contract."
        )
