"""Application settings, environment secret loading, and log redaction filter.

Security invariants:
1. Secrets (GEMINI_API_KEY, SEC_USER_AGENT, OPENFIGI_API_KEY) come ONLY from
   environment variables via Settings. YAML config files cannot supply secrets,
   and `.env` files are never opened or read directly.
2. Settings.__repr__ and Settings.__str__ always mask secret values.
3. Only GeminiProvider reads the Gemini API key.
4. SecretRedactionFilter scrubs literal secrets and credential patterns from all logs.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REDACTED_MASK = "***REDACTED***"

DEFAULT_FUNDS: dict[str, str] = {
    "Berkshire Hathaway": "0001067983",
    "Renaissance Technologies": "0001037389",
    "Citadel Advisors": "0001423053",
    "Two Sigma Investments": "0001179392",
    "Bridgewater Associates": "0001350694",
    "CalPERS": "0000919079",
    "Temasek Holdings": "0001021944",
}

_SECRET_YAML_KEYS = frozenset(
    {
        "gemini_api_key",
        "sec_user_agent",
        "openfigi_api_key",
        "api_key",
        "secret",
        "token",
        "password",
    }
)

# Regex patterns for redacting credential patterns in log messages
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(gemini[_-]?api[_-]?key|openfigi[_-]?api[_-]?key|api[_-]?key|access[_-]?token|"
    r"auth[_-]?token|key|token|secret|password|passwd)"
    r"(\s*[=:]\s*['\"]?)"
    r"([^\s&'\",;}\]]+)"
)
_AUTH_HEADER_PATTERN = re.compile(
    r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?(?:bearer\s+|basic\s+|token\s+|apikey\s+)?)"
    r"([^\s'\",;}\]]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-~+/]+=*)")
_HEADER_KEY_PATTERN = re.compile(
    r"(?i)(x-goog-api-key|x-openfigi-apikey|sec[_-]?user[_-]?agent)"
    r"(['\"]?\s*[:=]\s*['\"]?)"
    r"([^'\",;}\]\r\n]+)"
)
_GOOGLE_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z\-_]{10,}")


def _parse_bool_env(value: str | None) -> bool:
    """Parse an environment variable string into a boolean."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def redact_text(text: str, literal_secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Redact literal secret values and common credential patterns from text."""
    if not text:
        return text

    redacted = text
    for secret in literal_secrets:
        cleaned = secret.strip() if secret else ""
        if len(cleaned) >= 4 and cleaned in redacted:
            redacted = redacted.replace(cleaned, REDACTED_MASK)

    redacted = _AUTH_HEADER_PATTERN.sub(rf"\g<1>{REDACTED_MASK}", redacted)
    redacted = _BEARER_PATTERN.sub(rf"\g<1> {REDACTED_MASK}", redacted)
    redacted = _HEADER_KEY_PATTERN.sub(rf"\g<1>\g<2>{REDACTED_MASK}", redacted)
    redacted = _KEY_VALUE_PATTERN.sub(rf"\g<1>\g<2>{REDACTED_MASK}", redacted)
    redacted = _GOOGLE_KEY_PATTERN.sub(REDACTED_MASK, redacted)
    return redacted


class SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts secret values and key/Authorization patterns."""

    def __init__(self, secrets: list[str] | tuple[str, ...] | None = None, name: str = "") -> None:
        super().__init__(name=name)
        self._explicit_secrets: list[str] = [
            s.strip() for s in (secrets or ()) if s and len(s.strip()) >= 4
        ]

    def add_secret(self, secret: str | None) -> None:
        """Register an additional secret string for literal redaction."""
        if secret and len(secret.strip()) >= 4:
            cleaned = secret.strip()
            if cleaned not in self._explicit_secrets:
                self._explicit_secrets.append(cleaned)

    def _collect_secrets(self) -> list[str]:
        """Combine explicitly registered secrets with current secret env vars."""
        combined = list(self._explicit_secrets)
        for env_name in ("GEMINI_API_KEY", "SEC_USER_AGENT", "OPENFIGI_API_KEY"):
            env_val = os.environ.get(env_name, "").strip()
            if len(env_val) >= 4 and env_val not in combined:
                combined.append(env_val)
        return combined

    def redact(self, text: str) -> str:
        """Redact all known secrets and credential patterns from a string."""
        return redact_text(text, self._collect_secrets())

    def filter(self, record: logging.LogRecord) -> bool:
        """Mutate the log record in place so no secret values are emitted."""
        try:
            formatted = record.getMessage()
        except Exception:
            formatted = str(record.msg)

        record.msg = self.redact(formatted)
        record.args = ()

        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


class SettingsError(ValueError):
    """Raised when configuration or secret invariants are violated."""


@dataclass(slots=True)
class Settings:
    """Centralized runtime settings.

    Secrets (`sec_user_agent`, `gemini_api_key`, `openfigi_api_key`) are loaded
    exclusively from environment variables and are always masked in `__repr__`
    and `__str__`.
    """

    # Secrets (loaded from environment variables; masked in __repr__)
    sec_user_agent: str = field(default="", repr=False)
    _gemini_api_key: str = field(default="", repr=False)
    openfigi_api_key: str = field(default="", repr=False)

    # Pipeline & completeness settings
    target_period: str = "2026-Q2"
    baseline_period: str = "2026-Q1"
    min_funds: int = 7
    allow_partial: bool = False
    max_validation_retries: int = 3
    state_dir: Path = field(default_factory=lambda: Path("state"))
    reports_dir: Path = field(default_factory=lambda: Path("reports"))
    prompts_dir: Path = field(default_factory=lambda: Path("prompts"))
    data_dir: Path = field(default_factory=lambda: Path("data"))
    db_path: Path = field(default_factory=lambda: Path("data/autopilot.db"))

    # SEC Gateway settings
    sec_base_url: str = "https://data.sec.gov"
    sec_edgar_archives_url: str = "https://www.sec.gov/Archives/edgar/data"
    sec_max_requests_per_second: float = 8.0
    sec_timeout_seconds: float = 15.0
    sec_max_retries: int = 3
    sec_backoff_base_seconds: float = 0.5
    sec_backoff_max_seconds: float = 30.0

    # LLM Gateway & Budget Guard settings
    llm_provider: str = "null"
    llm_model: str = "gemini-2.5-flash"
    llm_max_calls_per_run: int = 5
    llm_max_output_tokens: int = 2048
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 3
    llm_backoff_base_seconds: float = 1.0
    llm_disabled: bool = False

    # Target funds mapping (Fund Name -> 10-digit CIK)
    funds: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FUNDS))

    def __init__(
        self,
        *,
        sec_user_agent: str | None = None,
        gemini_api_key: str | None = None,
        openfigi_api_key: str | None = None,
        target_period: str = "2026-Q2",
        baseline_period: str = "2026-Q1",
        min_funds: int | None = None,
        allow_partial: bool = False,
        max_validation_retries: int = 3,
        state_dir: Path | str = Path("state"),
        reports_dir: Path | str = Path("reports"),
        prompts_dir: Path | str = Path("prompts"),
        data_dir: Path | str = Path("data"),
        db_path: Path | str | None = None,
        sec_base_url: str = "https://data.sec.gov",
        sec_edgar_archives_url: str = "https://www.sec.gov/Archives/edgar/data",
        sec_max_requests_per_second: float = 8.0,
        sec_timeout_seconds: float = 15.0,
        sec_max_retries: int = 3,
        sec_backoff_base_seconds: float = 0.5,
        sec_backoff_max_seconds: float = 30.0,
        llm_provider: str = "null",
        llm_model: str = "gemini-2.5-flash",
        llm_max_calls_per_run: int = 5,
        llm_max_output_tokens: int = 2048,
        llm_timeout_seconds: float = 30.0,
        llm_max_retries: int = 3,
        llm_backoff_base_seconds: float = 1.0,
        llm_disabled: bool | None = None,
        funds: dict[str, str] | None = None,
    ) -> None:
        self.sec_user_agent = (
            os.environ.get("SEC_USER_AGENT", "") if sec_user_agent is None else sec_user_agent
        ).strip()
        self._gemini_api_key = (
            os.environ.get("GEMINI_API_KEY", "") if gemini_api_key is None else gemini_api_key
        ).strip()
        self.openfigi_api_key = (
            os.environ.get("OPENFIGI_API_KEY", "") if openfigi_api_key is None else openfigi_api_key
        ).strip()

        self.funds = dict(DEFAULT_FUNDS) if funds is None else dict(funds)
        self.target_period = target_period
        self.baseline_period = baseline_period
        self.min_funds = len(self.funds) if min_funds is None else int(min_funds)
        self.allow_partial = bool(allow_partial)
        self.max_validation_retries = int(max_validation_retries)
        self.state_dir = Path(state_dir)
        self.reports_dir = Path(reports_dir)
        self.prompts_dir = Path(prompts_dir)
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path) if db_path is not None else self.data_dir / "autopilot.db"

        self.sec_base_url = sec_base_url.rstrip("/")
        self.sec_edgar_archives_url = sec_edgar_archives_url.rstrip("/")
        # Enforce hard ceiling of 8 requests/sec for SEC gateway
        self.sec_max_requests_per_second = min(float(sec_max_requests_per_second), 8.0)
        self.sec_timeout_seconds = float(sec_timeout_seconds)
        self.sec_max_retries = int(sec_max_retries)
        self.sec_backoff_base_seconds = float(sec_backoff_base_seconds)
        self.sec_backoff_max_seconds = float(sec_backoff_max_seconds)

        self.llm_provider = llm_provider.strip().lower()
        self.llm_model = llm_model.strip()
        self.llm_max_calls_per_run = int(llm_max_calls_per_run)
        self.llm_max_output_tokens = int(llm_max_output_tokens)
        self.llm_timeout_seconds = float(llm_timeout_seconds)
        self.llm_max_retries = int(llm_max_retries)
        self.llm_backoff_base_seconds = float(llm_backoff_base_seconds)
        self.llm_disabled = (
            _parse_bool_env(os.environ.get("AUTOPILOT_LLM_DISABLED"))
            if llm_disabled is None
            else bool(llm_disabled)
        )

    @property
    def has_gemini_key(self) -> bool:
        """Return True if a non-empty Gemini API key is configured (without exposing it)."""
        return bool(self._gemini_api_key)

    def read_gemini_api_key(self) -> str:
        """Return the Gemini API key. Intended exclusively for `GeminiProvider`."""
        return self._gemini_api_key

    def secret_values(self) -> list[str]:
        """Return non-empty secret values for registration with `SecretRedactionFilter`."""
        return [
            s
            for s in (self.sec_user_agent, self._gemini_api_key, self.openfigi_api_key)
            if s and len(s) >= 4
        ]

    def __repr__(self) -> str:
        """Return a representation with all secret fields masked."""
        sec_ua_mask = REDACTED_MASK if self.sec_user_agent else "<unset>"
        gemini_mask = REDACTED_MASK if self._gemini_api_key else "<unset>"
        openfigi_mask = REDACTED_MASK if self.openfigi_api_key else "<unset>"
        return (
            f"Settings("
            f"sec_user_agent={sec_ua_mask!r}, "
            f"gemini_api_key={gemini_mask!r}, "
            f"openfigi_api_key={openfigi_mask!r}, "
            f"target_period={self.target_period!r}, "
            f"baseline_period={self.baseline_period!r}, "
            f"min_funds={self.min_funds!r}, "
            f"allow_partial={self.allow_partial!r}, "
            f"llm_provider={self.llm_provider!r}, "
            f"llm_model={self.llm_model!r}, "
            f"llm_max_calls_per_run={self.llm_max_calls_per_run!r}, "
            f"llm_max_output_tokens={self.llm_max_output_tokens!r}, "
            f"llm_disabled={self.llm_disabled!r}, "
            f"sec_max_requests_per_second={self.sec_max_requests_per_second!r}"
            f")"
        )

    def __str__(self) -> str:
        """Ensure string conversion also masks secrets."""
        return self.__repr__()

    @classmethod
    def from_env(
        cls,
        config_path: Path | str | None = None,
        *,
        load_dotenv_file: bool = False,
    ) -> Settings:
        """Load non-secret settings from YAML (if present) and secrets from environment.

        Never opens or reads `.env` files (`load_dotenv_file` is ignored/False by design).
        """
        _ = load_dotenv_file
        resolved_path = Path(config_path or os.environ.get("AUTOPILOT_CONFIG_PATH", "config.yaml"))
        raw_cfg: dict[str, Any] = {}
        if resolved_path.exists():
            with resolved_path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
                if not isinstance(loaded, dict):
                    raise SettingsError(f"Expected mapping in {resolved_path}, got {type(loaded)}")
                raw_cfg = loaded

        # Reject any secret keys placed in YAML config
        cls._reject_yaml_secrets(raw_cfg, resolved_path)

        pipeline_cfg = raw_cfg.get("pipeline", {}) or {}
        sec_cfg = raw_cfg.get("sec", {}) or {}
        llm_cfg = raw_cfg.get("llm", {}) or {}
        funds_cfg = raw_cfg.get("funds")
        resolved_funds = dict(funds_cfg) if isinstance(funds_cfg, dict) else dict(DEFAULT_FUNDS)

        provider_override = os.environ.get("AUTOPILOT_LLM_PROVIDER")
        provider = (
            provider_override
            if provider_override is not None
            else str(llm_cfg.get("provider", "null"))
        )

        data_dir = Path(pipeline_cfg.get("data_dir", "data"))
        raw_db_path = pipeline_cfg.get("db_path")
        resolved_db_path = Path(raw_db_path) if raw_db_path else (data_dir / "autopilot.db")
        raw_min_funds = pipeline_cfg.get("min_funds")
        resolved_min_funds = (
            int(raw_min_funds) if raw_min_funds is not None else len(resolved_funds)
        )

        return cls(
            sec_user_agent=os.environ.get("SEC_USER_AGENT", ""),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            openfigi_api_key=os.environ.get("OPENFIGI_API_KEY", ""),
            target_period=str(pipeline_cfg.get("target_period", "2026-Q2")),
            baseline_period=str(pipeline_cfg.get("baseline_period", "2026-Q1")),
            min_funds=resolved_min_funds,
            allow_partial=bool(pipeline_cfg.get("allow_partial", False)),
            max_validation_retries=int(pipeline_cfg.get("max_validation_retries", 3)),
            state_dir=Path(pipeline_cfg.get("state_dir", "state")),
            reports_dir=Path(pipeline_cfg.get("reports_dir", "reports")),
            prompts_dir=Path(pipeline_cfg.get("prompts_dir", "prompts")),
            data_dir=data_dir,
            db_path=resolved_db_path,
            sec_base_url=str(sec_cfg.get("base_url", "https://data.sec.gov")),
            sec_edgar_archives_url=str(
                sec_cfg.get("edgar_archives_url", "https://www.sec.gov/Archives/edgar/data")
            ),
            sec_max_requests_per_second=float(sec_cfg.get("max_requests_per_second", 8.0)),
            sec_timeout_seconds=float(sec_cfg.get("timeout_seconds", 15.0)),
            sec_max_retries=int(sec_cfg.get("max_retries", 3)),
            sec_backoff_base_seconds=float(sec_cfg.get("backoff_base_seconds", 0.5)),
            sec_backoff_max_seconds=float(sec_cfg.get("backoff_max_seconds", 30.0)),
            llm_provider=provider,
            llm_model=str(llm_cfg.get("model", "gemini-2.5-flash")),
            llm_max_calls_per_run=int(llm_cfg.get("max_calls_per_run", 5)),
            llm_max_output_tokens=int(llm_cfg.get("max_output_tokens", 2048)),
            llm_timeout_seconds=float(llm_cfg.get("timeout_seconds", 30.0)),
            llm_max_retries=int(llm_cfg.get("max_retries", 3)),
            llm_backoff_base_seconds=float(llm_cfg.get("backoff_base_seconds", 1.0)),
            llm_disabled=_parse_bool_env(os.environ.get("AUTOPILOT_LLM_DISABLED")),
            funds=resolved_funds,
        )

    @classmethod
    def _reject_yaml_secrets(cls, cfg: dict[str, Any], path: Path) -> None:
        """Ensure secrets are never supplied through YAML files."""
        for key, value in cfg.items():
            norm_key = str(key).strip().lower()
            if norm_key in _SECRET_YAML_KEYS and value:
                raise SettingsError(
                    f"Secret key {key!r} found in {path}; secrets must come only from env vars."
                )
            if isinstance(value, dict):
                cls._reject_yaml_secrets(value, path)


def configure_logging(
    settings: Settings | None = None,
    *,
    level: int = logging.INFO,
    logger_name: str | None = None,
) -> SecretRedactionFilter:
    """Configure logging with `SecretRedactionFilter` attached to logger and handlers."""
    secrets = settings.secret_values() if settings is not None else []
    redaction_filter = SecretRedactionFilter(secrets=secrets)

    target_logger = logging.getLogger(logger_name)
    target_logger.setLevel(level)
    target_logger.addFilter(redaction_filter)

    if not target_logger.handlers and logger_name is None:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        target_logger.addHandler(handler)

    for handler in target_logger.handlers:
        handler.addFilter(redaction_filter)

    return redaction_filter
