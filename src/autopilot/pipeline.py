"""Pipeline orchestrator: executes ordered stages and records `RunLog` timing and status."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from autopilot.models import (
    PipelineContext,
    RunLog,
    StageLogEntry,
    StageStatus,
)
from autopilot.settings import Settings
from autopilot.stages import (
    DetectStage,
    FactsStage,
    IngestStage,
    NarrateStage,
    PublishStage,
    ReviewStage,
    Stage,
    StageError,
    StageResult,
    StageSkip,
    ValidateStage,
)

__all__ = [
    "Pipeline",
    "PipelineContext",
    "RunLog",
    "Stage",
    "StageError",
    "StageLogEntry",
    "StageResult",
    "StageSkip",
    "StageStatus",
    "build_default_stages",
]

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return current UTC timestamp formatted as ISO-8601."""
    return datetime.now(UTC).isoformat()


def build_default_stages() -> list[Stage]:
    """Return the canonical ordered sequence of 13F autopilot pipeline stages."""
    return [
        DetectStage(),
        IngestStage(),
        FactsStage(),
        NarrateStage(),
        ValidateStage(),
        ReviewStage(),
        PublishStage(),
    ]


class Pipeline:
    """Executes an ordered sequence of `Stage` instances and records a `RunLog`."""

    def __init__(
        self,
        stages: Sequence[Stage] | None = None,
        *,
        settings: Settings | None = None,
        clock_fn: Callable[[], float] = time.perf_counter,
        timestamp_fn: Callable[[], str] = _utc_now_iso,
        stop_on_failure: bool = True,
    ) -> None:
        self._settings = settings or Settings()
        self.stages: list[Stage] = list(stages) if stages is not None else build_default_stages()
        self._clock_fn = clock_fn
        self._timestamp_fn = timestamp_fn
        self.stop_on_failure = stop_on_failure

    @staticmethod
    def _normalize_outcome(outcome: StageResult | StageStatus | None) -> StageResult:
        """Convert stage return value into a normalized `StageResult`."""
        if outcome is None:
            return StageResult(status=StageStatus.SUCCEEDED)
        if isinstance(outcome, StageStatus):
            return StageResult(status=outcome)
        if isinstance(outcome, StageResult):
            return outcome
        raise TypeError(f"Unsupported stage return type: {type(outcome).__name__}")

    def run(
        self,
        context: PipelineContext | None = None,
        *,
        run_id: str | None = None,
        persist_path: Path | str | None = None,
    ) -> RunLog:
        """Execute stages in order and return a populated `RunLog` with per-stage timing."""
        ctx = (
            context
            if context is not None
            else PipelineContext(
                target_period=self._settings.target_period,
                baseline_period=self._settings.baseline_period,
            )
        )
        resolved_run_id = run_id or uuid.uuid4().hex[:12]
        pipeline_start_perf = self._clock_fn()
        run_log = RunLog(
            run_id=resolved_run_id,
            target_period=ctx.target_period,
            started_at=self._timestamp_fn(),
            status=StageStatus.RUNNING,
        )

        logger.info(
            "Pipeline run started: run_id=%s period=%s stage_count=%d",
            run_log.run_id,
            run_log.target_period,
            len(self.stages),
        )

        for stage in self.stages:
            stage_name = getattr(stage, "name", stage.__class__.__name__.lower())
            stage_start_iso = self._timestamp_fn()
            stage_start_perf = self._clock_fn()

            try:
                if not stage.should_run(ctx):
                    duration_ms = max(0.0, (self._clock_fn() - stage_start_perf) * 1000.0)
                    entry = StageLogEntry(
                        stage_name=stage_name,
                        status=StageStatus.SKIPPED,
                        started_at=stage_start_iso,
                        finished_at=self._timestamp_fn(),
                        duration_ms=duration_ms,
                        attempts=0,
                        message="Skipped via should_run(context) == False",
                    )
                    run_log.stages.append(entry)
                    logger.info(
                        "Stage skipped: stage=%s status=%s duration_ms=%.2f",
                        stage_name,
                        entry.status,
                        duration_ms,
                    )
                    continue

                raw_outcome = stage.run(ctx)
                result = self._normalize_outcome(raw_outcome)
                duration_ms = max(0.0, (self._clock_fn() - stage_start_perf) * 1000.0)
                entry = StageLogEntry(
                    stage_name=stage_name,
                    status=result.status,
                    started_at=stage_start_iso,
                    finished_at=self._timestamp_fn(),
                    duration_ms=duration_ms,
                    attempts=result.attempts,
                    message=result.message,
                    metadata=dict(result.metadata),
                )
                run_log.stages.append(entry)
                logger.info(
                    "Stage completed: stage=%s status=%s attempts=%d duration_ms=%.2f",
                    stage_name,
                    entry.status,
                    entry.attempts,
                    duration_ms,
                )

                if result.status == StageStatus.AWAITING_REVIEW:
                    run_log.status = StageStatus.AWAITING_REVIEW
                    break

                if result.status == StageStatus.FAILED:
                    run_log.status = StageStatus.FAILED
                    if self.stop_on_failure:
                        break

            except StageSkip as skip_exc:
                duration_ms = max(0.0, (self._clock_fn() - stage_start_perf) * 1000.0)
                entry = StageLogEntry(
                    stage_name=stage_name,
                    status=StageStatus.SKIPPED,
                    started_at=stage_start_iso,
                    finished_at=self._timestamp_fn(),
                    duration_ms=duration_ms,
                    attempts=1,
                    message=str(skip_exc),
                )
                run_log.stages.append(entry)
                logger.info(
                    "Stage skipped via StageSkip: stage=%s duration_ms=%.2f",
                    stage_name,
                    duration_ms,
                )

            except Exception as exc:
                duration_ms = max(0.0, (self._clock_fn() - stage_start_perf) * 1000.0)
                error_summary = f"{type(exc).__name__}: {exc}"
                entry = StageLogEntry(
                    stage_name=stage_name,
                    status=StageStatus.FAILED,
                    started_at=stage_start_iso,
                    finished_at=self._timestamp_fn(),
                    duration_ms=duration_ms,
                    attempts=1,
                    error=error_summary,
                )
                run_log.stages.append(entry)
                run_log.status = StageStatus.FAILED
                logger.error(
                    "Stage failed: stage=%s status=FAILED duration_ms=%.2f error=%s",
                    stage_name,
                    duration_ms,
                    error_summary,
                )
                if self.stop_on_failure:
                    break

        run_log.finished_at = self._timestamp_fn()
        run_log.total_duration_ms = max(0.0, (self._clock_fn() - pipeline_start_perf) * 1000.0)
        if run_log.status == StageStatus.RUNNING:
            run_log.status = StageStatus.SUCCEEDED

        if persist_path is not None:
            out_path = Path(persist_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(run_log.to_dict(), indent=2),
                encoding="utf-8",
            )

        logger.info(
            "Pipeline run finished: run_id=%s status=%s total_duration_ms=%.2f",
            run_log.run_id,
            run_log.status,
            run_log.total_duration_ms,
        )
        return run_log
