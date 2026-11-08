"""Unit tests for Settings secret masking, env-only loading, and log redaction filter."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from autopilot.settings import (
    REDACTED_MASK,
    SecretRedactionFilter,
    Settings,
    SettingsError,
    configure_logging,
    redact_text,
)


def test_settings_masks_secrets_in_repr_and_str() -> None:
    """Settings.__repr__ and __str__ must never expose secret values."""
    fake_gemini = "test-gemini-secret-value-9999"
    fake_ua = "Jane Doe jane.doe@institutional-research.org"
    fake_figi = "openfigi-secret-token-8888"

    settings = Settings(
        sec_user_agent=fake_ua,
        gemini_api_key=fake_gemini,
        openfigi_api_key=fake_figi,
    )

    rendered_repr = repr(settings)
    rendered_str = str(settings)

    assert fake_gemini not in rendered_repr
    assert fake_ua not in rendered_repr
    assert fake_figi not in rendered_repr
    assert fake_gemini not in rendered_str
    assert fake_ua not in rendered_str
    assert REDACTED_MASK in rendered_repr


def test_settings_loads_secrets_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Secrets come from environment variables while non-secrets load from YAML."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "pipeline:\n  target_period: '2026-Q3'\nllm:\n  provider: 'null'\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SEC_USER_AGENT", "Analyst Bot analyst@example.org")
    monkeypatch.setenv("GEMINI_API_KEY", "synth-key-value-12345")

    settings = Settings.from_env(config_path=cfg_file, load_dotenv_file=False)

    assert settings.target_period == "2026-Q3"
    assert settings.sec_user_agent == "Analyst Bot analyst@example.org"
    assert settings.has_gemini_key is True
    assert settings.read_gemini_api_key() == "synth-key-value-12345"
    assert "synth-key-value-12345" not in repr(settings)


def test_settings_rejects_secrets_in_yaml(tmp_path: Path) -> None:
    """Placing secret keys inside config.yaml must raise SettingsError."""
    bad_cfg = tmp_path / "bad_config.yaml"
    bad_cfg.write_text(
        "llm:\n  gemini_api_key: 'should-not-be-in-yaml'\n",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match="secrets must come only from env vars"):
        Settings.from_env(config_path=bad_cfg, load_dotenv_file=False)


def test_log_redaction_filter_scrubs_secrets_and_patterns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SecretRedactionFilter must redact literal secrets, key=..., and Authorization."""
    literal_secret = "super-sensitive-credential-xyz987"
    ua_secret = "Researcher secret.agent@fund.org"
    settings = Settings(
        sec_user_agent=ua_secret,
        gemini_api_key=literal_secret,
    )

    test_logger = logging.getLogger("autopilot.test_redaction")
    test_logger.handlers.clear()
    redaction_filter = configure_logging(
        settings,
        level=logging.INFO,
        logger_name="autopilot.test_redaction",
    )
    assert isinstance(redaction_filter, SecretRedactionFilter)

    with caplog.at_level(logging.INFO, logger="autopilot.test_redaction"):
        test_logger.info("Connecting with literal %s and UA %s", literal_secret, ua_secret)
        test_logger.info(
            "Query params url=https://example.com/v1?key=my_query_key_123&api_key=abc999"
        )
        test_logger.info("Headers: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
        test_logger.info("Header x-goog-api-key: goog_secret_header_token")

    # Ensure caplog records and formatted messages contain zero raw secrets
    for record in caplog.records:
        redaction_filter.filter(record)
        msg = record.getMessage()
        assert literal_secret not in msg
        assert ua_secret not in msg
        assert "my_query_key_123" not in msg
        assert "abc999" not in msg
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in msg
        assert "goog_secret_header_token" not in msg
        assert REDACTED_MASK in msg


def test_redact_text_patterns_directly() -> None:
    """redact_text scrubs key=..., Authorization, Bearer, and literal secrets."""
    raw = (
        "GET /items?key=secretKey001&token=tok_222 "
        "Authorization: Bearer bearer_tok_333 "
        "custom=literal_val_444"
    )
    cleaned = redact_text(raw, literal_secrets=["literal_val_444"])
    assert "secretKey001" not in cleaned
    assert "tok_222" not in cleaned
    assert "bearer_tok_333" not in cleaned
    assert "literal_val_444" not in cleaned
    assert cleaned.count(REDACTED_MASK) == 4
