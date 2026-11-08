"""Shared pytest fixtures enforcing 100% offline execution and clean environment state."""

from __future__ import annotations

import pytest
import requests


@pytest.fixture(autouse=True)
def _isolate_env_and_block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear secret env vars and block unmocked network sockets in every test."""
    for var in (
        "SEC_USER_AGENT",
        "GEMINI_API_KEY",
        "OPENFIGI_API_KEY",
        "AUTOPILOT_LLM_DISABLED",
        "AUTOPILOT_LLM_PROVIDER",
        "AUTOPILOT_CONFIG_PATH",
    ):
        monkeypatch.delenv(var, raising=False)

    def _blocked_send(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "Network access is forbidden in tests. Mock requests.Session or HTTP adapter."
        )

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked_send)
