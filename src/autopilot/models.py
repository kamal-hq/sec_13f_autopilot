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
class SecFilingMetadata:
    """Metadata describing an institutional manager's SEC Form 13F filing."""

    cik: str
    fund_name: str
    accession_number: str
    form_type: str
    filing_date: str
    report_date: str
    period: str
    primary_document: str = "primary_doc.xml"
    info_table_document: str = ""


@dataclass(slots=True)
class DetectionResult:
    """Output contract of the DetectStage."""

    period: str
    new_filings: list[SecFilingMetadata] = field(default_factory=list)
    already_ingested: list[str] = field(default_factory=list)
    has_updates: bool = False


@dataclass(slots=True)
class IngestResult:
    """Output contract of the IngestStage."""

    period: str
    ingested_accessions: list[str] = field(default_factory=list)
    db_path: str = ""
    holdings_row_count: int = 0


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


@dataclass(slots=True)
class FactsBundle:
    """Output contract of the FactsStage: single source of truth for narrative numbers."""

    period: str
    baseline_period: str
    generated_at: str
    funds: list[FundSummary] = field(default_factory=list)
    convergence_signals: list[ConvergenceSignal] = field(default_factory=list)
    allowed_numbers: set[str] = field(default_factory=set)
    raw_metrics: dict[str, Any] = field(default_factory=dict)


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
    dry_run: bool = False
    detection: DetectionResult | None = None
    ingest: IngestResult | None = None
    facts: FactsBundle | None = None
    draft: NarrativeDraft | None = None
    validation: ValidationResult | None = None
    review: ReviewDecision | None = None
    publish: PublishResult | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
