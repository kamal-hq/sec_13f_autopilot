"""Domain models, stage contracts, and pipeline execution logs for sec_13f_autopilot."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class StageStatus(StrEnum):
    """Execution status of an individual pipeline stage or overall pipeline run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SKIPPED = "SKIPPED"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    FAILED = "FAILED"


class PeriodStatus(StrEnum):
    """Lifecycle status of a quarterly 13F reporting period ('YYYY-QN')."""

    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    PROCESSED = "PROCESSED"
    RESTATED = "RESTATED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(slots=True)
class StageLogEntry:
    """Timing and execution status record for a single pipeline stage."""

    stage_name: str
    status: StageStatus
    started_at: str
    finished_at: str | None = None
    duration_ms: float = 0.0
    attempts: int = 1
    message: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize stage entry into a JSON-compatible dictionary."""
        data = asdict(self)
        data["status"] = str(self.status)
        return data


@dataclass(slots=True)
class RunLog:
    """Structured execution log capturing per-stage timing and status for a pipeline run."""

    run_id: str
    target_period: str
    started_at: str
    finished_at: str | None = None
    total_duration_ms: float = 0.0
    status: StageStatus = StageStatus.PENDING
    stages: list[StageLogEntry] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """Return True if the pipeline completed without failure."""
        return self.status in {StageStatus.SUCCEEDED, StageStatus.AWAITING_REVIEW}

    def get_stage(self, stage_name: str) -> StageLogEntry | None:
        """Look up a recorded stage entry by name."""
        for entry in self.stages:
            if entry.stage_name == stage_name:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize run log into a JSON-compatible dictionary."""
        return {
            "run_id": self.run_id,
            "target_period": self.target_period,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_duration_ms": self.total_duration_ms,
            "status": str(self.status),
            "stages": [stage.to_dict() for stage in self.stages],
        }


@dataclass(frozen=True, slots=True)
class Filing:
    """Metadata describing an institutional manager's SEC Form 13F-HR or 13F-HR/A filing."""

    cik: str
    fund_name: str
    accession: str
    form_type: str
    filed_date: str
    period: str
    amendment_type: str | None = None
    report_date: str = ""
    primary_document: str = "primary_doc.xml"
    infotable_document: str = ""
    table_entry_total: int | None = None
    table_value_total: int | None = None
    supersedes: tuple[str, ...] = ()

    @property
    def accession_number(self) -> str:
        """Backwards-compatible alias for `accession`."""
        return self.accession

    @property
    def filing_date(self) -> str:
        """Backwards-compatible alias for `filed_date`."""
        return self.filed_date

    def to_dict(self) -> dict[str, Any]:
        """Serialize filing into a JSON-compatible dictionary."""
        return {
            "cik": self.cik,
            "fund_name": self.fund_name,
            "accession": self.accession,
            "form_type": self.form_type,
            "filed_date": self.filed_date,
            "period": self.period,
            "amendment_type": self.amendment_type,
            "report_date": self.report_date,
            "primary_document": self.primary_document,
            "infotable_document": self.infotable_document,
            "table_entry_total": self.table_entry_total,
            "table_value_total": self.table_value_total,
            "supersedes": list(self.supersedes),
        }


# Backwards-compatible alias used in Phase 0 gateway helpers
SecFilingMetadata = Filing


@dataclass(slots=True)
class Period:
    """Aggregated filing state and completeness evaluation for a reporting quarter ('YYYY-QN')."""

    period: str
    status: PeriodStatus
    filings: list[Filing] = field(default_factory=list)
    new_filings: list[Filing] = field(default_factory=list)
    already_processed: list[str] = field(default_factory=list)
    missing_funds: list[str] = field(default_factory=list)
    superseded_accessions: dict[str, list[str]] = field(default_factory=dict)
    changelog: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    reason: str = ""
    actionable: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize period summary into a JSON-compatible dictionary."""
        return {
            "period": self.period,
            "status": str(self.status),
            "actionable": self.actionable,
            "reason": self.reason,
            "filings": [f.to_dict() for f in self.filings],
            "new_filings": [f.to_dict() for f in self.new_filings],
            "already_processed": list(self.already_processed),
            "missing_funds": list(self.missing_funds),
            "superseded_accessions": {
                acc: list(sup) for acc, sup in self.superseded_accessions.items()
            },
            "changelog": list(self.changelog),
            "review_reasons": list(self.review_reasons),
        }


@dataclass(slots=True)
class DetectResult:
    """Output contract of the DetectStage."""

    period: str = "2026-Q2"
    as_of: str | None = None
    periods: dict[str, Period] = field(default_factory=dict)
    new_filings: list[Filing] = field(default_factory=list)
    already_ingested: list[str] = field(default_factory=list)
    actionable_periods: list[str] = field(default_factory=list)
    has_updates: bool = False
    noop_reason: str | None = None
    report_paths: dict[str, str] = field(default_factory=dict)

    def get_period(self, period_name: str) -> Period | None:
        """Look up a `Period` record by its `YYYY-QN` identifier."""
        return self.periods.get(period_name)

    def to_dict(self) -> dict[str, Any]:
        """Serialize detection result into a JSON-compatible dictionary."""
        return {
            "period": self.period,
            "as_of": self.as_of,
            "has_updates": self.has_updates,
            "noop_reason": self.noop_reason,
            "actionable_periods": list(self.actionable_periods),
            "new_filings": [f.to_dict() for f in self.new_filings],
            "already_ingested": list(self.already_ingested),
            "periods": {k: v.to_dict() for k, v in sorted(self.periods.items())},
            "report_paths": dict(self.report_paths),
        }


# Backwards-compatible alias
DetectionResult = DetectResult


@dataclass(frozen=True, slots=True)
class FundReconciliation:
    """Cover-page `<tableValueTotal>` reconciliation result for a single fund-period filing."""

    cik: str
    fund_name: str
    period: str
    accession: str
    form_type: str
    amendment_type: str | None
    raw_rows: int
    aggregated_rows: int
    db_total_value_usd: int
    cover_page_total_usd: int
    diff_usd: int
    pct_diff: float
    reconciled: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize reconciliation record to a dictionary."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DetectedSplit:
    """Stock split candidate detected during ingestion (confirmed or unconfirmed)."""

    cusip: str
    ticker: str | None
    company_name: str | None
    split_factor: float
    split_type: str
    detection_source: str
    confirmation_status: str
    split_date: str | None = None
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize split record to a dictionary."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AmendmentApplied:
    """Record of a 13F-HR/A restatement applied during ingestion."""

    cik: str
    fund_name: str
    period: str
    accession: str
    amendment_type: str
    filed_date: str
    supersedes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize amendment record to a dictionary."""
        data = asdict(self)
        data["supersedes"] = list(self.supersedes)
        return data


@dataclass(slots=True)
class IngestResult:
    """Output contract of the IngestStage."""

    period: str
    previous_period: str = ""
    loaded_periods: list[str] = field(default_factory=list)
    db_path: str = "data/autopilot.db"
    ingested_accessions: list[str] = field(default_factory=list)
    holdings_row_count: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    reconciliations: list[FundReconciliation] = field(default_factory=list)
    detected_splits: list[DetectedSplit] = field(default_factory=list)
    confirmed_splits: list[DetectedSplit] = field(default_factory=list)
    unconfirmed_splits: list[DetectedSplit] = field(default_factory=list)
    amendments_applied: list[AmendmentApplied] = field(default_factory=list)
    all_reconciled: bool = True
    report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize ingestion result into a JSON-compatible dictionary."""
        return {
            "period": self.period,
            "previous_period": self.previous_period,
            "loaded_periods": list(self.loaded_periods),
            "db_path": self.db_path,
            "ingested_accessions": list(self.ingested_accessions),
            "holdings_row_count": self.holdings_row_count,
            "row_counts": dict(self.row_counts),
            "all_reconciled": self.all_reconciled,
            "reconciliations": [r.to_dict() for r in self.reconciliations],
            "detected_splits": [s.to_dict() for s in self.detected_splits],
            "confirmed_splits": [s.to_dict() for s in self.confirmed_splits],
            "unconfirmed_splits": [s.to_dict() for s in self.unconfirmed_splits],
            "amendments_applied": [a.to_dict() for a in self.amendments_applied],
            "report_path": self.report_path,
        }


@dataclass(frozen=True, slots=True)
class FactItem:
    """A single deterministic, provenance-tagged fact in `reports/<period>/facts.json`."""

    id: str
    kind: str
    value: int | float
    display: tuple[str, ...]
    period: str
    source: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize fact item with stable sorted keys."""
        return {
            "display": list(self.display),
            "id": self.id,
            "kind": self.kind,
            "period": self.period,
            "source": dict(sorted(self.source.items())),
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class FundSummary:
    """Summary metrics for a single institutional fund in a reporting period."""

    cik: str
    fund_name: str
    fund_type: str
    period: str
    accession_number: str
    total_value_usd: int
    position_count: int
    equity_value_usd: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize fund summary into a dictionary."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConvergenceSignal:
    """Quantitative Smart Money Convergence signal for an operating issuer."""

    cusip_prefix: str
    ticker: str
    company_name: str
    sector: str
    qualifying_buyer_count: int
    buyer_funds: tuple[str, ...] = ()
    total_added_value_usd: int = 0
    aggregate_value_usd: int = 0
    hedge_fund_buyers: int = 0
    discretionary_buyers: int = 0
    buyer_mix: str = ""
    fund_type_breakdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize convergence signal into a dictionary."""
        data = asdict(self)
        data["buyer_funds"] = list(self.buyer_funds)
        return data


@dataclass(slots=True)
class FactsBundle:
    """Output contract of the FactsStage: single source of truth for narrative numbers."""

    period: str
    baseline_period: str
    generated_at: str
    facts: list[FactItem] = field(default_factory=list)
    funds: list[FundSummary] = field(default_factory=list)
    convergence_signals: list[ConvergenceSignal] = field(default_factory=list)
    allowed_numbers: set[str] = field(default_factory=set)
    raw_metrics: dict[str, Any] = field(default_factory=dict)
    diff_markdown: str = ""
    report_path: str | None = None
    diff_path: str | None = None
    run_path: str | None = None

    @property
    def previous_period(self) -> str:
        """Alias for baseline_period."""
        return self.baseline_period

    @property
    def signal_count(self) -> int:
        """Total broad convergence signals."""
        return len(self.convergence_signals)

    @property
    def discretionary_signal_count(self) -> int:
        """Total discretionary convergence signals."""
        fact = self.get_fact("market.signals_discretionary")
        return int(fact.value) if fact is not None else 0

    @property
    def total_aum_equity_usd(self) -> int:
        """Total equity-only AUM in USD."""
        fact = self.get_fact("market.aum_equity")
        return int(fact.value) if fact is not None else 0

    def get_fact(self, fact_id: str) -> FactItem | None:
        """Look up a `FactItem` by its semantic `id`."""
        for item in self.facts:
            if item.id == fact_id:
                return item
        return None

    def facts_by_kind(self) -> dict[str, int]:
        """Return count of facts grouped by `kind` (sorted by kind name)."""
        counts: dict[str, int] = {}
        for item in self.facts:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return dict(sorted(counts.items()))

    def to_facts_json_dict(self) -> dict[str, Any]:
        """Return the deterministic, timestamp-free dictionary for `reports/<period>/facts.json`."""
        payload = dict(self.raw_metrics)
        payload["period"] = self.period
        payload["baseline_period"] = self.baseline_period
        payload["facts"] = [f.to_dict() for f in self.facts]
        payload["allowed_numbers"] = sorted(self.allowed_numbers)
        return payload


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Normalized result returned by any LLMProvider implementation."""

    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "STOP"


@dataclass(slots=True)
class NarrativeDraft:
    """Output contract of the NarrateStage."""

    period: str
    markdown: str
    llm_result: LLMResult
    attempt: int = 1


@dataclass(slots=True)
class ValidationResult:
    """Output contract of the ValidateStage."""

    passed: bool
    attempt: int = 1
    unverified_numbers: list[str] = field(default_factory=list)
    missing_sections: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReviewDecision:
    """Output contract of the ReviewStage."""

    approved: bool
    pr_number: int | None = None
    reviewer: str | None = None
    notes: str = ""


@dataclass(slots=True)
class PublishResult:
    """Output contract of the PublishStage."""

    period: str
    published_files: list[str] = field(default_factory=list)
    commit_sha: str | None = None


@dataclass(slots=True)
class PipelineContext:
    """Mutable shared state passed sequentially through ordered pipeline stages."""

    target_period: str
    baseline_period: str = "2026-Q1"
    as_of: str | None = None
    dry_run: bool = False
    detection: DetectResult | None = None
    ingest: IngestResult | None = None
    facts: FactsBundle | None = None
    draft: NarrativeDraft | None = None
    validation: ValidationResult | None = None
    review: ReviewDecision | None = None
    publish: PublishResult | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
