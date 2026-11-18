"""Offline unit tests for Phase 5: PublishStage, PR body builder, learn feedback, and metrics."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from autopilot.metrics import (
    compute_edit_ratio,
    format_metrics_markdown,
    learn_from_period,
    parse_baseline_md,
)
from autopilot.models import FactsBundle, PipelineContext
from autopilot.settings import Settings
from autopilot.stages.narrate import build_narrative_prompt
from autopilot.stages.publish import (
    PublishStage,
    build_pr_body,
    render_final_clean,
)


@pytest.fixture
def publish_env(tmp_path: Path) -> dict[str, Any]:
    """Create an isolated temporary environment populated with verified 2026-Q2 artifacts."""
    reports_dir = tmp_path / "reports"
    state_dir = tmp_path / "state"
    period_dir = reports_dir / "2026-Q2"
    period_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    fixtures_2026_q2 = Path(__file__).parent / "fixtures" / "2026_q2"
    for fname in ("facts.json", "draft.md", "diff.md", "run.json", "validation_report.json"):
        src = fixtures_2026_q2 / fname
        assert src.exists(), f"Missing tracked 2026-Q2 fixture: {src}"
        shutil.copyfile(src, period_dir / fname)

    settings = Settings(
        reports_dir=reports_dir,
        state_dir=state_dir,
        prompts_dir=Path("prompts"),
        target_period="2026-Q2",
        baseline_period="2026-Q1",
    )
    facts = FactsBundle.from_file(period_dir / "facts.json")
    return {
        "facts": facts,
        "period_dir": period_dir,
        "reports_dir": reports_dir,
        "settings": settings,
        "state_dir": state_dir,
        "tmp_path": tmp_path,
    }


def test_publish_without_commit_state_leaves_state_untouched_and_writes_exports(
    publish_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publish without --commit-state leaves state untouched and writes final.md and CSV exports."""
    fake_secret = "AIzaSyD-FAKE-KEY-PUBLISH-TEST-999999"
    monkeypatch.setenv("GEMINI_API_KEY", fake_secret)

    settings: Settings = publish_env["settings"]
    period_dir: Path = publish_env["period_dir"]
    state_dir: Path = publish_env["state_dir"]

    stage = PublishStage(settings=settings, commit_state=False, replay=False)
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1")
    res = stage.run(ctx)

    assert res.metadata["state_committed"] is False
    assert not (state_dir / "processed_filings.json").exists(), (
        "state/processed_filings.json must remain untouched without --commit-state"
    )

    # Check required files under reports/2026-Q2/
    final_md_path = period_dir / "final.md"
    final_cited_path = period_dir / "final_cited.md"
    val_md_path = period_dir / "validation_report.md"
    diff_path = period_dir / "diff.md"
    run_path = period_dir / "run.json"

    for p in (final_md_path, final_cited_path, val_md_path, diff_path, run_path):
        assert p.exists(), f"Missing expected published file: {p}"

    final_md = final_md_path.read_text(encoding="utf-8")
    # Clean render: no backticks around figures, short display forms such as "$917.2B"
    assert "$917.2B" in final_md
    assert "`$917" not in final_md
    assert "[market.aum_equity]" not in final_md

    # Cited render retains citations
    final_cited = final_cited_path.read_text(encoding="utf-8")
    assert "[market.aum_equity]" in final_cited

    # Check exports/ CSVs
    exports_dir = period_dir / "exports"
    for csv_name in ("signals.csv", "discretionary.csv", "new_to_sample.csv", "fund_summary.csv"):
        csv_path = exports_dir / csv_name
        assert csv_path.exists(), f"Missing CSV export: {csv_name}"
        with csv_path.open("r", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        assert len(rows) >= 2, f"Expected header + data rows in {csv_name}"

    # Verify no secret in run.json
    run_text = run_path.read_text(encoding="utf-8")
    assert fake_secret not in run_text


def test_publish_with_commit_state_updates_processed_filings(
    publish_env: dict[str, Any],
) -> None:
    """publish --commit-state marks the period's 7 accessions as processed in state."""
    settings: Settings = publish_env["settings"]
    state_dir: Path = publish_env["state_dir"]

    stage = PublishStage(settings=settings, commit_state=True, replay=False)
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1", commit_state=True)
    res = stage.run(ctx)

    assert res.metadata["state_committed"] is True
    state_path = state_dir / "processed_filings.json"
    assert state_path.exists()

    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    filings = state_data.get("filings") or {}
    assert len(filings) == 7
    assert "0001104659-26-104387" in filings
    assert filings["0001104659-26-104387"]["period"] == "2026-Q2"


def test_publish_replay_labels_outputs_and_never_touches_state(
    publish_env: dict[str, Any],
) -> None:
    """--replay labels outputs 'replay' and never touches state even if commit_state=True."""
    settings: Settings = publish_env["settings"]
    period_dir: Path = publish_env["period_dir"]
    state_dir: Path = publish_env["state_dir"]

    # Pass both commit_state=True and replay=True: replay must win and never touch state
    stage = PublishStage(settings=settings, commit_state=True, replay=True)
    ctx = PipelineContext(
        target_period="2026-Q2",
        baseline_period="2026-Q1",
        commit_state=True,
        replay=True,
    )
    res = stage.run(ctx)

    assert res.metadata["replay"] is True
    assert res.metadata["state_committed"] is False
    assert not (state_dir / "processed_filings.json").exists(), (
        "--replay must never create or modify state/processed_filings.json"
    )

    final_md = (period_dir / "final.md").read_text(encoding="utf-8")
    final_cited = (period_dir / "final_cited.md").read_text(encoding="utf-8")
    run_data = json.loads((period_dir / "run.json").read_text(encoding="utf-8"))

    assert "[REPLAY]" in final_md
    assert "replay" in final_md.lower()
    assert "[REPLAY]" in final_cited
    assert run_data["replay"] is True
    assert run_data["mode_label"] == "replay"


def test_pr_body_builder_contains_all_required_sections(publish_env: dict[str, Any]) -> None:
    """build_pr_body includes diff summary, validation counts, metrics, checklist, and links."""
    period_dir: Path = publish_env["period_dir"]
    diff_md = (period_dir / "diff.md").read_text(encoding="utf-8")
    val_dict = json.loads((period_dir / "validation_report.json").read_text(encoding="utf-8"))
    run_meta = {
        "llm": {
            "attempts": 2,
            "final_status": "success",
            "llm_mode": "gemini",
            "model": "gemini-test-model",
            "validation_failures_per_rule": {"R1": 1},
        },
        "stage_seconds": {
            "detect": 0.45,
            "facts": 1.12,
            "ingest": 2.30,
            "narrate": 3.40,
            "publish": 0.15,
            "validate": 0.08,
        },
    }

    body = build_pr_body(
        period="2026-Q2",
        diff_markdown=diff_md,
        validation_report=val_dict,
        run_metadata=run_meta,
        replay=False,
    )

    # 1. Diff summary
    assert "Quarter-over-Quarter Diff Summary" in body
    assert "GOOGL/GOOG" in body

    # 2. Validation PASS/FAIL with rule counts R1-R9
    assert "Deterministic Validation Audit: **PASS**" in body
    for r_idx in range(1, 10):
        assert f"**R{r_idx}**" in body

    # 3. Metrics table (mode, model, attempts, stage timings)
    assert "`gemini`" in body
    assert "`gemini-test-model`" in body
    assert "| **Narrative Attempts** | `2` |" in body
    assert "| `ingest` | `2.300s` |" in body

    # 4. Reviewer checklist (4 mandatory imperative items, all unchecked)
    assert "- [ ] **Spot-check 3 cited figures against the filings XML or cover pages**" in body
    assert "- [ ] **Check issuer names against nameOfIssuer**" in body
    assert "- [ ] **Confirm all required limitations are present**" in body
    assert "- [ ] **Confirm no predictive language or ungrounded causal claims**" in body
    assert "- [x]" not in body.lower()

    # 5. Artifact links
    for rel_link in (
        "reports/2026-Q2/final.md",
        "reports/2026-Q2/final_cited.md",
        "reports/2026-Q2/validation_report.md",
        "reports/2026-Q2/diff.md",
        "reports/2026-Q2/run.json",
        "reports/2026-Q2/exports/signals.csv",
        "reports/2026-Q2/exports/discretionary.csv",
        "reports/2026-Q2/exports/new_to_sample.csv",
        "reports/2026-Q2/exports/fund_summary.csv",
    ):
        assert f"({rel_link})" in body


def test_learn_computes_edit_ratio_and_copies_style_example(
    publish_env: dict[str, Any],
) -> None:
    """autopilot learn computes edit ratio on synthetic edits, writes history, and copies style."""
    settings: Settings = publish_env["settings"]
    period_dir: Path = publish_env["period_dir"]
    tmp_path: Path = publish_env["tmp_path"]

    # First publish so final_cited.md exists
    PublishStage(settings=settings).run(PipelineContext(target_period="2026-Q2"))

    bot_draft = (period_dir / "draft.md").read_text(encoding="utf-8")
    assert compute_edit_ratio(bot_draft, bot_draft) == 0.0

    # Apply synthetic human reviewer edits to final_cited.md
    edited_note = bot_draft.replace(
        "## Executive Summary",
        "## Executive Summary\n\nHuman-reviewed commentary added during PR inspection.",
    )
    (period_dir / "final_cited.md").write_text(edited_note, encoding="utf-8")

    history_file = tmp_path / "metrics" / "history.jsonl"
    style_dir = tmp_path / "style_examples"
    baseline_file = tmp_path / "baseline.md"
    baseline_file.write_text(
        "# Baseline\n- T1_minutes_per_fund: \n- T2_minutes_per_fund: \n- T3_minutes: \n",
        encoding="utf-8",
    )

    rec = learn_from_period(
        settings=settings,
        period="2026-Q2",
        review_minutes=None,
        baseline_path=baseline_file,
        history_path=history_file,
        style_examples_dir=style_dir,
    )

    assert 0.0 < rec["edit_ratio"] < 0.25
    assert rec["manual_baseline_minutes"] is None
    assert rec["review_minutes"] is None
    assert history_file.exists()
    assert (style_dir / "2026-Q2_final_cited.md").exists()

    # Verify prompt builder includes at most one style example when multiple exist
    (style_dir / "2026-Q1_final_cited.md").write_text("Older example", encoding="utf-8")
    prompt = build_narrative_prompt(
        publish_env["facts"],
        Path("prompts"),
        "2026-Q2",
        style_examples_dir=style_dir,
    )
    assert prompt.count("## Approved Style Example (Reference Only):") == 1
    assert "Older example" not in prompt


def test_metrics_table_not_measured_yet_and_time_saved_rule(tmp_path: Path) -> None:
    """metrics table outputs 'not measured yet' for empty fields and omits % unless both exist."""
    history_file = tmp_path / "history.jsonl"
    baseline_file = tmp_path / "baseline.md"
    baseline_file.write_text(
        "# Baseline\n- T1_minutes_per_fund: \n- T2_minutes_per_fund: \n- T3_minutes: \n",
        encoding="utf-8",
    )
    assert parse_baseline_md(baseline_file) is None

    # Case 1: Record with missing review_minutes and missing manual_baseline_minutes
    rec1 = {
        "attempts": 1,
        "edit_ratio": 0.012,
        "manual_baseline_minutes": None,
        "mode": "none",
        "model": "deterministic-template",
        "period": "2026-Q2",
        "review_minutes": None,
        "stage_seconds": {"facts": 1.2, "narrate": 0.1},
        "total_stage_seconds": 1.3,
        "validation_failures_by_rule": {},
    }
    # Case 2: Record with review_minutes ONLY (still must NOT compute time-saved %)
    rec2 = {
        **rec1,
        "period": "2026-Q1",
        "review_minutes": 8.0,
        "manual_baseline_minutes": None,
    }
    history_file.write_text(
        json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n",
        encoding="utf-8",
    )

    table_unmeasured = format_metrics_markdown(history_file, baseline_file)
    assert "not measured yet" in table_unmeasured
    assert "%" not in table_unmeasured, (
        "No time-saved percentage allowed unless both review_minutes and manual_baseline exist"
    )

    # Case 3: Populate baseline.md with numeric T1, T2, T3 -> now rec2 has BOTH numbers
    baseline_file.write_text(
        "# Baseline\n- T1_minutes_per_fund: 5\n- T2_minutes_per_fund: 5\n- T3_minutes: 10\n",
        encoding="utf-8",
    )
    # 7 * (5 + 5) + 10 = 80.0 min; review = 8.0 min -> 90.0% saved for rec2, rec1 stays unmeasured
    assert parse_baseline_md(baseline_file) == 80.0
    table_measured = format_metrics_markdown(history_file, baseline_file)
    lines = table_measured.splitlines()
    q2_row = next(line for line in lines if "2026-Q2" in line)
    q1_row = next(line for line in lines if "2026-Q1" in line)
    assert "%" not in q2_row
    assert "not measured yet" in q2_row
    assert "90.0%" in q1_row


def test_render_final_clean_short_usd_and_no_backticks(publish_env: dict[str, Any]) -> None:
    """render_final_clean strips figure backticks and uses validator-accepted short displays."""
    facts: FactsBundle = publish_env["facts"]
    sample = (
        "- **Equity-only AUM**: `$917,153,241,621 [market.aum_equity]` "
        "across `7 [coverage.funds_filed]` funds [coverage.all_reconciled].\n"
    )
    rendered = render_final_clean(sample, facts, replay=False)
    assert rendered.strip() == "- **Equity-only AUM**: $917.2B across 7 funds."


def test_detect_emit_period_selection_and_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """detect --emit-period selects most recent COMPLETE/RESTATED with baseline;
    never falls back.
    """
    from autopilot.cli import main
    from autopilot.models import DetectResult, Period, PeriodStatus
    from autopilot.stages.detect import DetectStage, select_actionable_period

    # Case 1: 2026-Q1 PROCESSED, 2026-Q2 COMPLETE, 2026-Q3 PARTIAL -> selects 2026-Q2
    res_q2 = DetectResult(
        period="",
        periods={
            "2026-Q1": Period(period="2026-Q1", status=PeriodStatus.PROCESSED, actionable=False),
            "2026-Q2": Period(period="2026-Q2", status=PeriodStatus.COMPLETE, actionable=True),
            "2026-Q3": Period(period="2026-Q3", status=PeriodStatus.PARTIAL, actionable=False),
        },
        actionable_periods=["2026-Q2"],
        has_updates=True,
    )
    assert select_actionable_period(res_q2) == "2026-Q2"

    # Case 2: 2026-Q1 PROCESSED, 2026-Q2 RESTATED -> selects 2026-Q2
    res_restated = DetectResult(
        period="",
        periods={
            "2026-Q1": Period(period="2026-Q1", status=PeriodStatus.PROCESSED, actionable=False),
            "2026-Q2": Period(period="2026-Q2", status=PeriodStatus.RESTATED, actionable=True),
        },
        actionable_periods=["2026-Q2"],
        has_updates=True,
    )
    assert select_actionable_period(res_restated) == "2026-Q2"

    # Case 3: 2026-Q1 COMPLETE but no 2025-Q4 baseline period available -> None
    res_no_baseline = DetectResult(
        period="",
        periods={
            "2026-Q1": Period(period="2026-Q1", status=PeriodStatus.COMPLETE, actionable=True),
        },
        actionable_periods=["2026-Q1"],
        has_updates=True,
    )
    assert select_actionable_period(res_no_baseline) is None

    # Case 4: All periods PROCESSED (even though config target_period is 2026-Q2) -> no fallback
    res_all_processed = DetectResult(
        period="",
        periods={
            "2026-Q1": Period(period="2026-Q1", status=PeriodStatus.PROCESSED, actionable=False),
            "2026-Q2": Period(period="2026-Q2", status=PeriodStatus.PROCESSED, actionable=False),
        },
        actionable_periods=[],
        has_updates=False,
    )
    assert select_actionable_period(res_all_processed) is None

    monkeypatch.setattr(DetectStage, "detect", lambda self, **kwargs: res_all_processed)
    rc = main(["detect", "--emit-period"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "no actionable period"


def test_seed_caches_resolve_all_2026_q2_cusips_with_zero_network_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an empty data dir and seed/ present, all 2026-Q2 CUSIPs resolve with 0 network calls."""
    import socket
    import urllib.request

    import requests
    from sec_13f_analyzer import map_cusips, map_sectors

    from autopilot.stages.ingest import SEED_CACHE_FILES, ensure_seed_caches

    seed_dir = Path("seed")
    for fname in SEED_CACHE_FILES:
        assert (seed_dir / fname).exists(), f"Missing tracked seed file: seed/{fname}"

    # Verify all 2026-Q2 CUSIPs from seed/cusip_cache.json
    seed_cusips = list(
        json.loads((seed_dir / "cusip_cache.json").read_text(encoding="utf-8")).keys()
    )
    assert len(seed_cusips) >= 8000

    empty_data_dir = tmp_path / "empty_data"
    mappings_dir = empty_data_dir / "mappings"
    assert not empty_data_dir.exists()

    # Copy seed into empty data/mappings
    seeded = ensure_seed_caches(mappings_dir, seed_dir)
    assert set(seeded) == set(SEED_CACHE_FILES)

    # Strictly forbid any network call
    def _forbid_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network call attempted during seeded CUSIP/sector resolution!")

    monkeypatch.setattr(requests.Session, "request", _forbid_network)
    monkeypatch.setattr(socket, "create_connection", _forbid_network)
    monkeypatch.setattr(urllib.request, "urlopen", _forbid_network)

    resolved_map = map_cusips(
        seed_cusips,
        cache_path=str(mappings_dir / "cusip_cache.json"),
        api_key="",
    )
    assert len(resolved_map) == len(seed_cusips)

    tickers = [
        str(v.get("ticker"))
        for v in resolved_map.values()
        if isinstance(v, dict) and v.get("ticker")
    ][:200]
    resolved_sectors = map_sectors(
        tickers,
        fetch_uncached=False,
        cache_path=str(mappings_dir / "sector_cache.json"),
        user_agent=None,
    )
    assert len(resolved_sectors) > 0


def test_seed_state_dry_run_default_and_yes_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """seed-state --through 2026-Q2 is dry-run by default and writes 14 accessions with --yes."""
    from autopilot.state import seed_state_through

    state_file = tmp_path / "state" / "processed_filings.json"

    # Default dry-run: prints what it will mark, does not write state_file
    entries_dry, summary_dry = seed_state_through(
        state_file,
        "2026-Q2",
        apply_writes=False,
        seed_dir=Path("seed"),
        reports_dir=tmp_path / "reports",
    )
    assert len(entries_dry) == 14
    assert "[DRY-RUN]" in summary_dry
    assert "0001104659-26-104387" in summary_dry
    assert not state_file.exists()

    # With --yes (apply_writes=True): writes state_file with all 14 Q1 and Q2 accessions
    entries_yes, summary_yes = seed_state_through(
        state_file,
        "2026-Q2",
        apply_writes=True,
        seed_dir=Path("seed"),
        reports_dir=tmp_path / "reports",
    )
    assert len(entries_yes) == 14
    assert "[APPLIED]" in summary_yes
    assert state_file.exists()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(saved["filings"]) == 14
    periods_marked = {v["period"] for v in saved["filings"].values()}
    assert periods_marked == {"2026-Q1", "2026-Q2"}
    _ = capsys


def test_q3_readiness_three_period_full_pipeline_offline(tmp_path: Path) -> None:
    """--period 2026-Q3 sets baseline 2026-Q2, loads Q1+Q2+Q3, computes diff,
    and passes validate.
    """
    import pandas as pd
    from sec_13f_analyzer import bulk_insert_holdings, create_schema
    from tests.test_facts import FIXTURE_FUNDS, _fixture_raw_rows

    from autopilot.models import FundReconciliation, IngestResult
    from autopilot.pipeline import Pipeline
    from autopilot.policies import compute_lookback_periods, compute_previous_period
    from autopilot.stages.facts import FactsStage
    from autopilot.stages.narrate import NarrateStage
    from autopilot.stages.review import ReviewStage
    from autopilot.stages.validate import ValidateStage

    target_period = "2026-Q3"
    baseline_period = compute_previous_period(target_period)
    assert baseline_period == "2026-Q2"
    assert compute_lookback_periods(target_period, 2) == ["2026-Q1", "2026-Q2"]

    # Map the 3-period synthetic rows (2025-Q4, 2026-Q1, 2026-Q2) -> (2026-Q1, 2026-Q2, 2026-Q3)
    period_shift = {
        "2025-Q4": "2026-Q1",
        "2026-Q1": "2026-Q2",
        "2026-Q2": "2026-Q3",
    }
    base_rows = _fixture_raw_rows(include_q4_2025=True)
    shifted_rows = [{**r, "period": period_shift[r["period"]]} for r in base_rows]

    data_dir = tmp_path / "data"
    db_path = data_dir / "autopilot.db"
    reports_dir = tmp_path / "reports"
    state_dir = tmp_path / "state"
    split_cache = data_dir / "mappings" / "split_cache.json"
    split_cache.parent.mkdir(parents=True, exist_ok=True)
    split_cache.write_text("{}", encoding="utf-8")

    create_schema(db_path=str(db_path))
    bulk_insert_holdings(
        pd.DataFrame(shifted_rows),
        db_path=str(db_path),
        split_cache_path=str(split_cache),
    )

    reconciliations: list[FundReconciliation] = []
    for p_idx, per in enumerate(("2026-Q1", "2026-Q2", "2026-Q3"), start=1):
        for f_idx, (fund_name, cik) in enumerate(sorted(FIXTURE_FUNDS.items()), start=1):
            f_rows = [r for r in shifted_rows if r["period"] == per and r["fund_name"] == fund_name]
            tot_val = sum(int(r["value_usd"]) for r in f_rows)
            reconciliations.append(
                FundReconciliation(
                    cik=cik,
                    fund_name=fund_name,
                    period=per,
                    accession=f"{cik}-26-000{p_idx}{f_idx:02d}",
                    form_type="13F-HR",
                    amendment_type=None,
                    raw_rows=len(f_rows),
                    aggregated_rows=len(f_rows),
                    db_total_value_usd=tot_val,
                    cover_page_total_usd=tot_val,
                    diff_usd=0,
                    pct_diff=0.0,
                    reconciled=True,
                )
            )

    ingest_res = IngestResult(
        period="2026-Q3",
        previous_period="2026-Q2",
        loaded_periods=["2026-Q1", "2026-Q2", "2026-Q3"],
        db_path=str(db_path),
        ingested_accessions=[r.accession for r in reconciliations],
        holdings_row_count=len(shifted_rows),
        reconciliations=reconciliations,
        all_reconciled=True,
    )

    settings = Settings(
        target_period="2026-Q3",
        baseline_period="2026-Q2",
        lookback_periods=2,
        min_funds=len(FIXTURE_FUNDS),
        funds=dict(FIXTURE_FUNDS),
        data_dir=data_dir,
        db_path=db_path,
        reports_dir=reports_dir,
        state_dir=state_dir,
        prompts_dir=Path("prompts"),
        llm_provider="null",
    )

    pipeline = Pipeline(
        stages=[
            FactsStage(settings=settings),
            NarrateStage(settings=settings),
            ValidateStage(settings=settings),
            ReviewStage(settings=settings),
            PublishStage(settings=settings, commit_state=True),
        ],
        settings=settings,
    )
    ctx = PipelineContext(
        target_period="2026-Q3",
        baseline_period="2026-Q2",
        ingest=ingest_res,
        commit_state=True,
    )
    run_log = pipeline.run(ctx, persist_path=state_dir / "last_run.json")

    assert run_log.succeeded is True
    assert ctx.facts is not None
    assert ctx.facts.period == "2026-Q3"
    assert ctx.facts.baseline_period == "2026-Q2"
    assert ctx.facts.raw_metrics["diff"]["available"] is True
    assert ctx.facts.get_fact("diff.signals_persisted") is not None
    assert ctx.validation is not None and ctx.validation.passed is True
    assert (reports_dir / "2026-Q3" / "final.md").exists()
    assert (reports_dir / "2026-Q3" / "pr_body.md").exists()
    assert (state_dir / "processed_filings.json").exists()
