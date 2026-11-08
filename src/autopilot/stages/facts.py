"""FactsStage stub: computes deterministic quantitative facts and Smart Money Convergence signals.

Stage Contract
--------------
- **Stage Name**: ``facts``
- **Input Contract**:
  - ``context.target_period`` (``str``) and ``context.baseline_period`` (``str``).
  - ``context.ingest`` (:class:`autopilot.models.IngestResult`): Provides ``db_path`` pointing
    to the populated ``sec_13f_analyzer`` SQLite database.
- **Output Contract**:
  - Writes ``context.facts`` as a :class:`autopilot.models.FactsBundle` containing:
    - ``period`` (``str``) and ``baseline_period`` (``str``).
    - ``generated_at`` (``str``): ISO-8601 UTC timestamp.
    - ``funds`` (``list[FundSummary]``): Per-fund AUM and position counts.
    - ``convergence_signals`` (``list[ConvergenceSignal]``): Issuers meeting the Smart Money
      Convergence criteria.
    - ``allowed_numbers`` (``set[str]``): Canonical whitelist of every numeric token (raw and
      formatted) derived from the SQLite database that ``ValidateStage`` permits in the draft.
    - ``raw_metrics`` (``dict[str, Any]``): Serialized tables for ``reports/<period>/facts.json``.
- **Side Effects & Invariants**:
  - Purely deterministic SQL/pandas aggregation via ``sec_13f_analyzer``; zero network or LLM calls.
  - Every figure that appears in the downstream research note MUST originate in ``FactsBundle``.
"""

from __future__ import annotations

from autopilot.models import PipelineContext, StageStatus
from autopilot.stages.base import Stage, StageResult


class FactsStage(Stage):
    """Stub stage that builds the verified `FactsBundle` from SQLite holdings."""

    name: str = "facts"

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute quantitative fact extraction (stub — see module docstring for contract)."""
        _ = context
        raise NotImplementedError(
            "FactsStage is a scaffold stub; see docstring for input/output contract."
        )
