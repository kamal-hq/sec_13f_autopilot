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

    for fname in ("facts.json", "draft.md", "diff.md", "run.json", "validation_report.json"):
        src = Path("reports/2026-Q2") / fname
        if src.exists():
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

    # 4. Reviewer checklist (4 mandatory items)
    assert "- [ ] **3 figures spot-checked against the filings**" in body
    assert "- [ ] **Issuer names checked**" in body
    assert "- [ ] **Limitations present**" in body
    assert "- [ ] **No predictive language**" in body

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
