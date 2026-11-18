"""Offline unit tests for Phase 4 LLM narrative retry loop and guardrails.

100% offline; zero network calls; uses FakeProvider and BudgetGuard exclusively.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autopilot.gateways.llm import (
    BudgetGuard,
    LLMBudgetExceededError,
    LLMProvider,
    LLMRateLimitError,
)
from autopilot.models import FactsBundle, LLMResult, PipelineContext
from autopilot.settings import Settings
from autopilot.stages.narrate import (
    NarrateStage,
    build_narrative_prompt,
    parse_llm_blocks,
)
from autopilot.stages.validate import ValidateStage


class FakeProvider(LLMProvider):
    """Offline fake provider returning canned responses or raising configured errors."""

    provider_name: str = "gemini"
    model: str = "fake-gemini-model"

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.prompts_received: list[str] = []
        self.calls: int = 0

    def generate(self, prompt: str, max_output_tokens: int = 2048) -> LLMResult:
        self.calls += 1
        self.prompts_received.append(prompt)
        if not self.responses:
            raise RuntimeError("FakeProvider ran out of configured responses")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return LLMResult(
            text=resp,
            provider="gemini",
            model="fake-gemini-model",
            prompt_tokens=40,
            output_tokens=20,
            total_tokens=60,
            latency_ms=12.0,
            finish_reason="STOP",
        )


@pytest.fixture
def test_env(tmp_path: Path) -> dict[str, Any]:
    """Prepare isolated directory environment with verified 2026-Q2 facts and template."""
    reports_dir = tmp_path / "reports"
    period_dir = reports_dir / "2026-Q2"
    period_dir.mkdir(parents=True)

    fixtures_2026_q2 = Path(__file__).parent / "fixtures" / "2026_q2"
    src_facts = fixtures_2026_q2 / "facts.json"
    src_run = fixtures_2026_q2 / "run.json"

    facts_text = src_facts.read_text(encoding="utf-8")
    run_text = src_run.read_text(encoding="utf-8")

    (period_dir / "facts.json").write_text(facts_text, encoding="utf-8")
    (period_dir / "run.json").write_text(run_text, encoding="utf-8")

    settings = Settings(
        reports_dir=reports_dir,
        prompts_dir=Path("prompts"),
        target_period="2026-Q2",
        baseline_period="2026-Q1",
        max_validation_retries=3,
    )
    facts = FactsBundle.from_file(period_dir / "facts.json")
    return {"facts": facts, "period_dir": period_dir, "settings": settings}


def test_good_first_attempt(test_env: dict[str, Any]) -> None:
    """1. Good first attempt succeeds in 1 attempt with full metrics recorded."""
    settings: Settings = test_env["settings"]
    facts: FactsBundle = test_env["facts"]
    period_dir: Path = test_env["period_dir"]

    good_resp = (
        "## Executive Summary\n"
        "During 2026-Q2, institutional common-equity AUM expanded to "
        "$917,153,241,621 [market.aum_equity]."
    )
    prov = FakeProvider([good_resp])
    stage = NarrateStage(settings=settings, provider=prov)
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1", facts=facts)

    stage.run(ctx)

    assert prov.calls == 1
    assert ctx.draft is not None
    assert ctx.draft.attempt == 1

    # Check attempt artifacts
    attempts_dir = period_dir / "attempts"
    assert (attempts_dir / "attempt_1_prompt.md").exists()
    assert (attempts_dir / "attempt_1_response.md").exists()
    assert (attempts_dir / "attempt_1_draft.md").exists()
    assert (attempts_dir / "attempt_1_findings.json").exists()

    # Check run.json
    run_data = json.loads((period_dir / "run.json").read_text(encoding="utf-8"))
    assert run_data["llm"]["attempts"] == 1
    assert run_data["llm"]["final_status"] == "success"
    assert run_data["llm"]["llm_mode"] == "gemini"
    assert run_data["llm"]["output_tokens"] == 20

    # Check draft.md and draft_clean.md
    draft_md = (period_dir / "draft.md").read_text(encoding="utf-8")
    assert "$917,153,241,621 [market.aum_equity]" in draft_md
    draft_clean = (period_dir / "draft_clean.md").read_text(encoding="utf-8")
    assert "$917,153,241,621" in draft_clean
    assert "[market.aum_equity]" not in draft_clean


def test_bad_then_good_attempts_2(test_env: dict[str, Any]) -> None:
    """2. Bad attempt repaired on attempt 2 after receiving validation feedback."""
    settings: Settings = test_env["settings"]
    facts: FactsBundle = test_env["facts"]
    period_dir: Path = test_env["period_dir"]

    bad_resp = (
        "## Executive Summary\n"
        "In 2026-Q3, institutional common-equity AUM was $999B [market.aum_equity]."
    )
    good_resp = (
        "## Executive Summary\n"
        "During 2026-Q2, institutional common-equity AUM expanded to "
        "$917,153,241,621 [market.aum_equity]."
    )

    prov = FakeProvider([bad_resp, good_resp])
    stage = NarrateStage(settings=settings, provider=prov)
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1", facts=facts)

    stage.run(ctx)

    assert prov.calls == 2
    attempts_dir = period_dir / "attempts"
    assert (attempts_dir / "attempt_1_repair_prompt.md").exists()
    assert (attempts_dir / "attempt_2_findings.json").exists()

    run_data = json.loads((period_dir / "run.json").read_text(encoding="utf-8"))
    assert run_data["llm"]["attempts"] == 2
    assert run_data["llm"]["final_status"] == "success"
    assert run_data["llm"]["validation_failures_per_rule"].get("R1", 0) >= 1


def test_three_bad_fallback_and_flagged(test_env: dict[str, Any]) -> None:
    """3. Three failed validation attempts trigger deterministic fallback and status flagging."""
    settings: Settings = test_env["settings"]
    facts: FactsBundle = test_env["facts"]
    period_dir: Path = test_env["period_dir"]

    bad_resp_1 = "## Executive Summary\nIn 2026-Q3, AUM was $999B [market.aum_equity]."
    bad_resp_2 = "## Executive Summary\nIn 2026-Q3, AUM was $888B [market.aum_equity]."
    bad_resp_3 = "## Executive Summary\nIn 2026-Q3, AUM was $777B [market.aum_equity]."

    prov = FakeProvider([bad_resp_1, bad_resp_2, bad_resp_3])
    stage = NarrateStage(settings=settings, provider=prov, max_attempts=3)
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1", facts=facts)

    stage.run(ctx)

    assert prov.calls == 3
    attempts_dir = period_dir / "attempts"
    assert (attempts_dir / "attempt_1_findings.json").exists()
    assert (attempts_dir / "attempt_2_findings.json").exists()
    assert (attempts_dir / "attempt_3_findings.json").exists()

    run_data = json.loads((period_dir / "run.json").read_text(encoding="utf-8"))
    assert run_data["llm"]["attempts"] == 3
    assert run_data["llm"]["final_status"] == "fallback_after_failed_validation"

    # Draft fell back to deterministic template
    draft_md = (period_dir / "draft.md").read_text(encoding="utf-8")
    assert "$999B" not in draft_md
    assert "*Generated deterministically from filings data; not investment advice.*" in draft_md


def test_repeated_429_backoff_then_fallback(test_env: dict[str, Any]) -> None:
    """4. Rate limit 429 exhaustion falls back cleanly to deterministic draft."""
    settings: Settings = test_env["settings"]
    facts: FactsBundle = test_env["facts"]
    period_dir: Path = test_env["period_dir"]

    prov = FakeProvider([LLMRateLimitError("Gemini API rate limit persisted (429)")])
    stage = NarrateStage(settings=settings, provider=prov)
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1", facts=facts)

    stage.run(ctx)

    run_data = json.loads((period_dir / "run.json").read_text(encoding="utf-8"))
    assert run_data["llm"]["final_status"] == "quota_exhaustion_fallback"

    draft_md = (period_dir / "draft.md").read_text(encoding="utf-8")
    assert "*Generated deterministically from filings data; not investment advice.*" in draft_md


def test_budget_guard_stops_at_max_calls(test_env: dict[str, Any]) -> None:
    """5. BudgetGuard halts execution when max calls limit is exceeded."""
    settings: Settings = test_env["settings"]
    facts: FactsBundle = test_env["facts"]

    bad_resp = "## Executive Summary\nIn 2026-Q3, AUM was $999B [market.aum_equity]."
    prov = FakeProvider([bad_resp, bad_resp])
    guarded = BudgetGuard(prov, max_calls_per_run=1)

    stage = NarrateStage(settings=settings, provider=guarded, max_attempts=3)
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1", facts=facts)

    with pytest.raises(LLMBudgetExceededError):
        stage.run(ctx)

    assert prov.calls == 1


def test_fake_key_in_environment_never_leaks(
    test_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """6. Fake secret API key in environment never leaks into reports or logs."""
    fake_key = "AIzaSyD-FAKE-SECRET-KEY-NEVER-LEAK-12345"
    monkeypatch.setenv("GEMINI_API_KEY", fake_key)

    settings: Settings = test_env["settings"]
    facts: FactsBundle = test_env["facts"]
    period_dir: Path = test_env["period_dir"]

    good_resp = (
        "## Executive Summary\n"
        "During 2026-Q2, institutional common-equity AUM expanded to "
        "$917,153,241,621 [market.aum_equity]."
    )
    prov = FakeProvider([good_resp])
    stage = NarrateStage(settings=settings, provider=prov)
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1", facts=facts)

    stage.run(ctx)

    # Check all files under period directory
    for fpath in period_dir.rglob("*"):
        if fpath.is_file():
            content = fpath.read_text(encoding="utf-8", errors="ignore")
            assert fake_key not in content, f"Secret leaked in file: {fpath}"

    # Check logs
    assert fake_key not in caplog.text, "Secret leaked in logs"


def test_prompt_contains_no_file_paths_env_or_raw_filings(test_env: dict[str, Any]) -> None:
    """7. Compact fact sheet and prompt contain zero paths, environment vars, or raw XML."""
    facts: FactsBundle = test_env["facts"]
    prompts_dir = Path("prompts")

    prompt = build_narrative_prompt(facts, prompts_dir, "2026-Q2")

    # Disallowed file path substrings
    assert "data/autopilot.db" not in prompt
    assert "data\\autopilot.db" not in prompt
    assert "reports/" not in prompt
    assert "reports\\" not in prompt

    # Disallowed environment variable names
    assert "GEMINI_API_KEY" not in prompt
    assert "SEC_USER_AGENT" not in prompt
    assert "OPENFIGI_API_KEY" not in prompt

    # Disallowed XML tags from raw filings
    assert "<informationTable" not in prompt
    assert "<nameOfIssuer" not in prompt
    assert "</XML>" not in prompt


def test_hostile_output_rejected_by_r9(test_env: dict[str, Any]) -> None:
    """8. Hostile output (URLs, Markdown links, HTML tags, code fences, length cap) fails R9."""
    settings: Settings = test_env["settings"]
    facts: FactsBundle = test_env["facts"]
    validator = ValidateStage(settings)

    base_draft_lines = [
        "# Quarterly 13F Research Note (2026-Q2)",
        "",
        "## Executive Summary",
        "",
    ]
    deterministic_sections = [
        "## Data and Coverage",
        "## Broad Convergence",
        "## Discretionary Convergence",
        "## New to the Sample",
        "## What Changed Since Last Quarter",
        "## Sector View",
        "## Data Quality",
        "## Limitations",
        "- Form 13F filings [limit.long_only].",
        "- Submitted up to 45 calendar days [limit.filing_lag_45d].",
        "- Consensus co-buying patterns [limit.no_forward_return_test].",
        "- Signals may represent arbitrage legs [limit.hedge_only_may_be_hedging].",
        "- The discretionary cohort comprises 3 allocators [limit.small_discretionary_sample].",
        "- Dollars added figures are estimated [limit.dollars_added_estimate].",
    ]

    hostile_cases = [
        ("URL", "Visit https://evil.com for financial disclosures."),
        ("Markdown link", "Check out [phishing link](https://phishing.example.com)."),
        ("HTML tag", "Embedded script injection <script>alert(1)</script>."),
        ("Code fence", "```python\nimport os\nos.system('echo hostile')\n```"),
        ("Length cap", "Word " * 160),
    ]

    for label, snippet in hostile_cases:
        draft = "\n".join([*base_draft_lines, snippet, "", *deterministic_sections])
        report = validator.validate_draft(draft, facts, "2026-Q2")
        assert report.status == "FAIL", f"Hostile snippet '{label}' unexpectedly passed validation"
        r9_issues = [iss for iss in report.issues if iss.rule_id == "R9"]
        assert len(r9_issues) > 0, f"Hostile snippet '{label}' was not caught by Rule R9"


def test_parse_llm_blocks() -> None:
    """Unit test for parse_llm_blocks with canonical headings and single blocks."""
    text_with_headings = (
        "## Executive Summary\nProse summary.\n\n"
        "## Broad Convergence\nProse broad signals commentary.\n"
    )
    blocks = parse_llm_blocks(text_with_headings)
    assert blocks["executive_summary"] == "Prose summary."
    assert blocks["broad_convergence"] == "Prose broad signals commentary."

    single_block = "Just a single prose paragraph without any headers."
    single_blocks = parse_llm_blocks(single_block)
    assert single_blocks["executive_summary"] == single_block
