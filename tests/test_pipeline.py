"""Unit tests for Pipeline ordered execution, RunLog timing/status, and stage contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from autopilot.models import PipelineContext, StageStatus
from autopilot.pipeline import (
    Pipeline,
    Stage,
    StageResult,
    build_default_stages,
)


class _DummyStage(Stage):
    """Test stage recording execution order."""

    def __init__(
        self,
        name: str,
        order_log: list[str],
        *,
        status: StageStatus = StageStatus.SUCCEEDED,
        run_predicate: bool = True,
        raise_error: bool = False,
    ) -> None:
        self.name = name
        self._order_log = order_log
        self._status = status
        self._run_predicate = run_predicate
        self._raise_error = raise_error

    def should_run(self, context: PipelineContext) -> bool:
        _ = context
        return self._run_predicate

    def run(self, context: PipelineContext) -> StageResult:
        self._order_log.append(self.name)
        context.artifacts[self.name] = "done"
        if self._raise_error:
            raise RuntimeError(f"{self.name} boom")
        return StageResult(status=self._status, message=f"{self.name} completed")


def test_pipeline_runs_stages_in_order_and_records_timing(tmp_path: Path) -> None:
    """Pipeline executes stages sequentially and populates RunLog with timing and status."""
    executed: list[str] = []
    fake_ticks = iter([10.0, 10.0, 10.25, 10.25, 10.30, 10.30, 10.80, 11.0])

    stages: list[Stage] = [
        _DummyStage("detect", executed),
        _DummyStage("ingest", executed, run_predicate=False),
        _DummyStage("facts", executed),
    ]
    pipeline = Pipeline(
        stages=stages,
        clock_fn=lambda: next(fake_ticks),
        timestamp_fn=lambda: "2026-11-08T06:00:00+00:00",
    )

    log_path = tmp_path / "state" / "last_run.json"
    run_log = pipeline.run(
        PipelineContext(target_period="2026-Q2"),
        run_id="run-001",
        persist_path=log_path,
    )

    assert executed == ["detect", "facts"]
    assert run_log.run_id == "run-001"
    assert run_log.status == StageStatus.SUCCEEDED
    assert run_log.succeeded is True
    assert len(run_log.stages) == 3

    detect_entry = run_log.get_stage("detect")
    ingest_entry = run_log.get_stage("ingest")
    facts_entry = run_log.get_stage("facts")

    assert detect_entry is not None and detect_entry.status == StageStatus.SUCCEEDED
    assert detect_entry.duration_ms == pytest.approx(250.0)
    assert ingest_entry is not None and ingest_entry.status == StageStatus.SKIPPED
    assert facts_entry is not None and facts_entry.status == StageStatus.SUCCEEDED
    assert log_path.exists()


def test_pipeline_halts_on_review_gate_and_failure() -> None:
    """Pipeline stops before publish when ReviewStage returns AWAITING_REVIEW or on failure."""
    executed: list[str] = []
    stages: list[Stage] = [
        _DummyStage("validate", executed),
        _DummyStage("review", executed, status=StageStatus.AWAITING_REVIEW),
        _DummyStage("publish", executed),
    ]
    pipeline = Pipeline(stages=stages)
    run_log = pipeline.run(PipelineContext(target_period="2026-Q2"))

    assert executed == ["validate", "review"]
    assert run_log.status == StageStatus.AWAITING_REVIEW
    assert run_log.get_stage("publish") is None

    # Test failure halting
    executed_fail: list[str] = []
    fail_pipeline = Pipeline(
        stages=[
            _DummyStage("detect", executed_fail, raise_error=True),
            _DummyStage("ingest", executed_fail),
        ]
    )
    fail_log = fail_pipeline.run(PipelineContext(target_period="2026-Q2"))
    assert executed_fail == ["detect"]
    assert fail_log.status == StageStatus.FAILED
    assert fail_log.succeeded is False
    detect_failed = fail_log.get_stage("detect")
    assert detect_failed is not None and "RuntimeError: detect boom" in (detect_failed.error or "")


def test_default_stage_contracts_and_remaining_stubs() -> None:
    """All 7 stages define input/output contracts; Phases 3-5 stages raise NotImplementedError."""
    stages = build_default_stages()
    assert [s.name for s in stages] == [
        "detect",
        "ingest",
        "facts",
        "narrate",
        "validate",
        "review",
        "publish",
    ]
    ctx = PipelineContext(target_period="2026-Q2")
    stub_names = {"narrate", "validate", "review", "publish"}
    for stage in stages:
        module_doc = __import__(stage.__class__.__module__, fromlist=["*"]).__doc__ or ""
        assert "Input Contract" in module_doc
        assert "Output Contract" in module_doc
        if stage.name in stub_names:
            with pytest.raises(NotImplementedError):
                stage.run(ctx)
