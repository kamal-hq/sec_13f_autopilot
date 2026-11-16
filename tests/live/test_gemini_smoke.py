"""Live smoke test for Gemini API connectivity.

SKIPPED BY DEFAULT.
Never run in automated test suites, offline workflows, or CI.
Only executed manually by an operator when RUN_LIVE_GEMINI=1 is explicitly set
and GEMINI_API_KEY is configured in the environment.
"""

from __future__ import annotations

import os

import pytest

from autopilot.gateways.llm import GeminiProvider
from autopilot.settings import Settings


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_GEMINI"),
    reason="Live Gemini API test skipped by default; set RUN_LIVE_GEMINI=1 to run",
)
def test_gemini_live_smoke() -> None:
    """Send tiny prompt ('Reply with OK') to verify live Gemini connectivity."""
    settings = Settings.from_env()
    assert settings.has_gemini_key, "GEMINI_API_KEY must be set in environment"

    provider = GeminiProvider(settings)
    result = provider.generate("Reply with OK", max_output_tokens=10)

    assert result.text.strip()
    assert result.prompt_tokens > 0
    assert result.output_tokens > 0
