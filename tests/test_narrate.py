"""Tests for NarrateStage, note template rendering, fact citations, and golden drafts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.test_facts import (
    LOCAL_AUTOPILOT_DB,
    REQUIRED_LIMITATION_IDS,
    _create_fixture_environment,
)

from autopilot.models import FactsBundle, PipelineContext
from autopilot.settings import Settings
from autopilot.stages.facts import FactsStage
from autopilot.stages.narrate import NarrateStage, render_template_note

GOLDEN_DRAFT_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden_draft_2026_q2.md"

REQUIRED_SECTIONS = (
    "Executive Summary",
    "Data and Coverage",
    "Broad Convergence",
    "Discretionary Convergence",
    "New to the Sample",
    "What Changed Since Last Quarter",
    "Sector View",
    "Data Quality",
    "Limitations",
)


def test_narrate_golden_file_draft_and_byte_identity(tmp_path: Path) -> None:
    """Golden-file test and byte-identical repeat runs for deterministic draft generation."""
    settings, _, ingest_res = _create_fixture_environment(tmp_path, include_q4_2025=True)
    facts_stage = FactsStage(settings=settings)
    bundle = facts_stage.build_facts(
        target_period="2026-Q2",
        ingest_result=ingest_res,
        write_reports=True,
    )

    narrate_stage = NarrateStage(settings=settings)
    ctx1 = PipelineContext(
        target_period="2026-Q2",
        artifacts={"facts_bundle": bundle},
        facts=bundle,
    )
    res1 = narrate_stage.run(ctx1)
    assert res1.status.value == "SUCCEEDED"

    draft_path = settings.reports_dir / "2026-Q2" / "draft.md"
    assert draft_path.exists()
    first_bytes = draft_path.read_bytes()
    first_text = draft_path.read_text(encoding="utf-8")

    # 1. Byte-identical repeat runs
    ctx2 = PipelineContext(
        target_period="2026-Q2",
        artifacts={"facts_bundle": bundle},
        facts=bundle,
    )
    res2 = narrate_stage.run(ctx2)
    assert res2.status.value == "SUCCEEDED"
    second_bytes = draft_path.read_bytes()
    assert first_bytes == second_bytes

    # 2. Check all 9 required sections in order
    last_idx = -1
    for section in REQUIRED_SECTIONS:
        idx = first_text.find(f"## {section}")
        assert idx != -1, f"Missing required section: {section}"
        assert idx > last_idx, f"Section out of order: {section}"
        last_idx = idx

    # 3. Mandatory disclaimer footer
    assert (
        "*Not investment advice; AI-drafted, figures checked against filings data.*" in first_text
    )

    # 4. Compare against committed golden file
    if not GOLDEN_DRAFT_FIXTURE_PATH.exists():
        GOLDEN_DRAFT_FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_DRAFT_FIXTURE_PATH.write_bytes(first_bytes)
    assert first_bytes == GOLDEN_DRAFT_FIXTURE_PATH.read_bytes()


def test_every_cited_fact_id_exists_in_facts_json(tmp_path: Path) -> None:
    """Every bracketed fact citation in draft.md must resolve to an existing fact in FactsBundle."""
    settings, _, ingest_res = _create_fixture_environment(tmp_path, include_q4_2025=True)
    facts_stage = FactsStage(settings=settings)
    bundle = facts_stage.build_facts(
        target_period="2026-Q2",
        ingest_result=ingest_res,
        write_reports=True,
    )

    narrate_stage = NarrateStage(settings=settings)
    ctx = PipelineContext(
        target_period="2026-Q2",
        artifacts={"facts_bundle": bundle},
        facts=bundle,
    )
    narrate_stage.run(ctx)

    draft_text = (settings.reports_dir / "2026-Q2" / "draft.md").read_text(encoding="utf-8")
    citations = re.findall(r"\[([a-zA-Z0-9_\.]+)\]", draft_text)
    assert len(citations) > 20, "Expected at least 20 fact citations in generated draft"

    missing_citations: list[str] = []
    for fact_id in citations:
        if bundle.get_fact(fact_id) is None:
            missing_citations.append(fact_id)

    assert not missing_citations, f"Found cited fact IDs not present in facts: {missing_citations}"


def test_every_figure_in_draft_has_a_citation(tmp_path: Path) -> None:
    """Every quantitative metric in the narrative text must be directly followed by [fact_id]."""
    settings, _, ingest_res = _create_fixture_environment(tmp_path, include_q4_2025=True)
    facts_stage = FactsStage(settings=settings)
    bundle = facts_stage.build_facts(
        target_period="2026-Q2",
        ingest_result=ingest_res,
        write_reports=True,
    )

    template_path = settings.prompts_dir / "note_template.md"
    draft_text = render_template_note(bundle, template_path=template_path)

    # Citation regex matching `value [fact.id]` or `value` [fact.id]
    citation_regex = re.compile(r"(`?[-+]?\$?[\d,]+(?:\.\d+)?%?`?)\s*\[([a-zA-Z0-9_\.]+)\]")

    # Known static descriptive phrases / non-metric terms containing numbers
    static_allowlist = {
        "13F",
        "13F-HR",
        "13F-HR/A",
        "4+ funds buying",
        "2+ of 3 discretionary funds",
        "3+ funds buying",
        "zero prior holders",
        "45-Day Reporting Lag",
        "45 calendar days",
        "3 institutional allocators",
        "2 funds",
        "-for-1 multiplier",
        "-for-1 candidate",
    }

    uncited_figures: list[str] = []
    for line in draft_text.splitlines():
        # Skip markdown headers, empty lines, and the standard disclaimer
        if line.startswith("#") or line.startswith("*Not investment advice") or not line.strip():
            continue

        clean_line = line
        for phrase in static_allowlist:
            clean_line = clean_line.replace(phrase, " ")

        # Replace dates (e.g., 2026-Q2, 2026-Q1, 2026-08-14)
        clean_line = re.sub(r"202\d-Q\d", " ", clean_line)
        clean_line = re.sub(r"202\d-\d{2}-\d{2}", " ", clean_line)

        # Remove properly cited numbers
        clean_line = citation_regex.sub(" ", clean_line)

        # Any remaining numbers are un-cited figures
        remaining = re.findall(r"[-+]?\$?\b\d+(?:,\d+)*(?:\.\d+)?%?\b", clean_line)
        if remaining:
            uncited_figures.extend([f"{tok} in {line}" for tok in remaining])

    assert not uncited_figures, "Found un-cited figures in draft:\n" + "\n".join(uncited_figures)


def test_required_limitations_cited_in_draft(tmp_path: Path) -> None:
    """The Limitations section must cite all 5 required limit.* facts."""
    settings, _, ingest_res = _create_fixture_environment(tmp_path, include_q4_2025=True)
    facts_stage = FactsStage(settings=settings)
    bundle = facts_stage.build_facts(
        target_period="2026-Q2",
        ingest_result=ingest_res,
        write_reports=True,
    )

    template_path = settings.prompts_dir / "note_template.md"
    draft_text = render_template_note(bundle, template_path=template_path)

    lim_idx = draft_text.find("## Limitations")
    assert lim_idx != -1
    lim_section = draft_text[lim_idx:]

    for lim_id in REQUIRED_LIMITATION_IDS:
        assert f"[{lim_id}]" in lim_section, f"Missing required limitation citation: [{lim_id}]"


@pytest.mark.skipif(
    not LOCAL_AUTOPILOT_DB.exists(),
    reason="Local data/autopilot.db not present; skipping 2026-Q2 acceptance note test.",
)
def test_local_acceptance_draft_2026_q2(tmp_path: Path) -> None:
    """Acceptance test verifying full 2026-Q2 research note draft on real DB facts."""
    repo_root = Path(__file__).resolve().parents[1]
    settings = Settings.from_env(config_path=repo_root / "config.yaml", load_dotenv_file=False)
    settings.db_path = LOCAL_AUTOPILOT_DB
    settings.data_dir = repo_root / "data"
    settings.reports_dir = tmp_path / "reports"

    # Copy real ingest and facts if present
    real_facts_json = repo_root / "reports" / "2026-Q2" / "facts.json"
    if real_facts_json.exists():
        facts_bundle = FactsBundle.from_file(real_facts_json)
    else:
        facts_stage = FactsStage(settings=settings)
        facts_bundle = facts_stage.build_facts(target_period="2026-Q2", write_reports=True)

    narrate_stage = NarrateStage(settings=settings)
    ctx = PipelineContext(
        target_period="2026-Q2",
        artifacts={"facts_bundle": facts_bundle},
        facts=facts_bundle,
    )
    res = narrate_stage.run(ctx)
    assert res.status.value == "SUCCEEDED"

    draft_path = settings.reports_dir / "2026-Q2" / "draft.md"
    draft_text = draft_path.read_text(encoding="utf-8")

    # 1. Check all 9 required sections
    for sec in REQUIRED_SECTIONS:
        assert f"## {sec}" in draft_text

    # 2. Check Alphabet discretionary convergence figures
    assert "$18,665,002,497 [discretionary.GOOGL_GOOG.dollars_added]" in draft_text
    assert (
        "$17,110,074,056 [discretionary.GOOGL_GOOG.BERKSHIRE_HATHAWAY.dollars_added]" in draft_text
    )
    assert "$1,554,928,442 [discretionary.GOOGL_GOOG.TEMASEK_HOLDINGS.dollars_added]" in draft_text

    # 3. Check Equity-only AUM and signals
    assert "$917,153,241,621 [market.aum_equity]" in draft_text
    assert "56 [market.signals_total]" in draft_text
    assert "31 [market.signals_hedge_only]" in draft_text
    assert "9 [market.signals_discretionary]" in draft_text

    # 4. Check all 5 limitations
    for lim_id in REQUIRED_LIMITATION_IDS:
        assert f"[{lim_id}]" in draft_text

    # 5. Check all citations exist in facts
    citations = re.findall(r"\[([a-zA-Z0-9_\.]+)\]", draft_text)
    for fid in citations:
        assert facts_bundle.get_fact(fid) is not None, f"Cited fact {fid} missing from bundle"
