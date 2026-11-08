"""Offline unit tests for LLMProvider implementations, BudgetGuard, and provider selection."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from autopilot.gateways.llm import (
    BudgetGuard,
    GeminiProvider,
    LLMBudgetExceededError,
    LLMConfigurationError,
    LLMDisabledError,
    LLMProvider,
    LLMRateLimitError,
    LLMTimeoutError,
    ManualProvider,
    NeedsManualInput,
    NullProvider,
    select_llm_provider,
)
from autopilot.models import LLMResult
from autopilot.settings import Settings


def test_budget_guard_enforces_max_calls_per_run_default_5() -> None:
    """BudgetGuard allows up to 5 calls by default and blocks the 6th call."""
    null_provider = NullProvider(period="2026-Q2")
    guard = BudgetGuard(null_provider)

    assert guard.max_calls_per_run == 5
    for idx in range(5):
        result = guard.generate(f"Prompt {idx}", max_output_tokens=256)
        assert result.provider == "null"

    assert guard.calls_made == 5
    assert guard.remaining_calls == 0

    with pytest.raises(LLMBudgetExceededError, match="5/5"):
        guard.generate("Prompt 6 (over budget)", max_output_tokens=256)


def test_budget_guard_enforces_max_output_tokens_and_timeout() -> None:
    """BudgetGuard rejects excessive max_output_tokens and times out slow calls."""
    null_provider = NullProvider()
    guard = BudgetGuard(null_provider, max_calls_per_run=3, max_output_tokens=512)

    with pytest.raises(LLMBudgetExceededError, match="max_output_tokens"):
        guard.generate("Prompt", max_output_tokens=1024)

    class SlowProvider(LLMProvider):
        def generate(self, prompt: str, max_output_tokens: int = 2048) -> LLMResult:
            time.sleep(0.25)
            return LLMResult(text="slow", provider="slow", model="slow")

    timeout_guard = BudgetGuard(SlowProvider(), timeout_seconds=0.05)
    with pytest.raises(LLMTimeoutError):
        timeout_guard.generate("Prompt", max_output_tokens=128)


def test_budget_guard_kill_switch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting AUTOPILOT_LLM_DISABLED=1 immediately blocks guarded LLM calls."""
    guard = BudgetGuard(NullProvider())
    monkeypatch.setenv("AUTOPILOT_LLM_DISABLED", "1")

    with pytest.raises(LLMDisabledError, match="AUTOPILOT_LLM_DISABLED"):
        guard.generate("Blocked prompt", max_output_tokens=128)


def test_manual_provider_writes_prompt_and_requires_response(tmp_path: Path) -> None:
    """ManualProvider writes prompt.md, raises NeedsManualInput if absent, then reads response."""
    provider = ManualProvider(reports_dir=tmp_path, period="2026-Q2")

    with pytest.raises(NeedsManualInput) as exc_info:
        provider.generate("Verify Q2 holdings facts.", max_output_tokens=512)

    assert exc_info.value.prompt_path == tmp_path / "2026-Q2" / "prompt.md"
    assert exc_info.value.response_path == tmp_path / "2026-Q2" / "response.md"
    assert exc_info.value.prompt_path.read_text(encoding="utf-8") == "Verify Q2 holdings facts."

    # Now supply the human response file
    exc_info.value.response_path.write_text(
        "# Verified 2026-Q2 Note\n\nAll figures checked.",
        encoding="utf-8",
    )
    result = provider.generate("Verify Q2 holdings facts.", max_output_tokens=512)
    assert result.provider == "manual"
    assert "Verified 2026-Q2 Note" in result.text


def test_gemini_provider_handles_429_with_backoff_and_reads_key() -> None:
    """GeminiProvider retries HTTP 429 with exponential backoff using official SDK client."""
    sleeps: list[float] = []
    mock_client = MagicMock()

    rate_limit_err = Exception("429 RESOURCE_EXHAUSTED: Quota exceeded")
    mock_response = SimpleNamespace(
        text="# Drafted 2026-Q2 Note",
        usage_metadata=SimpleNamespace(
            prompt_token_count=120,
            candidates_token_count=45,
            total_token_count=165,
        ),
    )
    mock_client.models.generate_content.side_effect = [
        rate_limit_err,
        mock_response,
    ]

    settings = Settings(
        gemini_api_key="synth-gemini-key-for-mock",
        llm_provider="gemini",
        llm_max_retries=2,
        llm_backoff_base_seconds=0.5,
    )
    provider = GeminiProvider(
        settings,
        client=mock_client,
        sleep_fn=sleeps.append,
    )

    result = provider.generate("Facts JSON payload", max_output_tokens=512)
    assert result.provider == "gemini"
    assert result.prompt_tokens == 120
    assert result.output_tokens == 45
    assert result.total_tokens == 165
    assert sleeps == [pytest.approx(0.5)]

    # Verify persistent 429 raises LLMRateLimitError
    mock_client.models.generate_content.side_effect = rate_limit_err
    with pytest.raises(LLMRateLimitError):
        provider.generate("Facts JSON payload", max_output_tokens=512)


def test_provider_selection_and_only_gemini_reads_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify provider selection logic and that only GeminiProvider reads the Gemini key."""
    read_spy_calls = 0
    original_read = Settings.read_gemini_api_key

    def spy_read_key(self: Settings) -> str:
        nonlocal read_spy_calls
        read_spy_calls += 1
        return original_read(self)

    monkeypatch.setattr(Settings, "read_gemini_api_key", spy_read_key)

    # 1. NullProvider selection does NOT read the Gemini key
    s_null = Settings(gemini_api_key="unused-key-1234", llm_provider="null")
    p_null = select_llm_provider(s_null)
    assert isinstance(p_null, NullProvider)
    assert read_spy_calls == 0

    # 2. ManualProvider selection does NOT read the Gemini key
    s_manual = Settings(
        gemini_api_key="unused-key-1234",
        llm_provider="manual",
        reports_dir=tmp_path,
    )
    p_manual = select_llm_provider(s_manual)
    assert isinstance(p_manual, ManualProvider)
    assert read_spy_calls == 0

    # 3. GeminiProvider selection DOES read the Gemini key
    s_gemini = Settings(gemini_api_key="active-key-5678", llm_provider="gemini")
    p_gemini = select_llm_provider(s_gemini, client=MagicMock())
    assert isinstance(p_gemini, GeminiProvider)
    assert read_spy_calls == 1

    # 4. Auto provider without key selects NullProvider without reading key
    s_auto_no_key = Settings(gemini_api_key="", llm_provider="auto")
    p_auto = select_llm_provider(s_auto_no_key)
    assert isinstance(p_auto, NullProvider)
    assert read_spy_calls == 1

    # 5. Kill switch blocks explicit gemini provider selection
    monkeypatch.setenv("AUTOPILOT_LLM_DISABLED", "1")
    with pytest.raises(LLMDisabledError):
        select_llm_provider(s_gemini, client=MagicMock())

    # 6. Unknown provider raises LLMConfigurationError
    monkeypatch.delenv("AUTOPILOT_LLM_DISABLED", raising=False)
    with pytest.raises(LLMConfigurationError):
        select_llm_provider(Settings(llm_provider="invalid-provider"))
