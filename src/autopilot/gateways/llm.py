"""LLM gateway: abstract provider, Gemini/Manual/Null providers, and BudgetGuard.

Security invariants:
1. Only `GeminiProvider` reads the Gemini API key via `settings.read_gemini_api_key()`.
2. Never log prompt bodies, response bodies, or API keys; log only status and token counts.
3. `BudgetGuard` enforces `max_calls_per_run` (default 5), `max_output_tokens`, `timeout`,
   and the `AUTOPILOT_LLM_DISABLED=1` kill switch environment variable.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from autopilot.models import LLMResult
from autopilot.settings import Settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base typed exception for LLM gateway operations."""


class LLMConfigurationError(LLMError):
    """Raised when an LLM provider is misconfigured or missing required credentials."""


class LLMDisabledError(LLMError):
    """Raised when an LLM call is attempted while AUTOPILOT_LLM_DISABLED=1 is active."""


class LLMBudgetExceededError(LLMError):
    """Raised when max calls per run or max output tokens budget is exceeded."""


class LLMRateLimitError(LLMError):
    """Raised when the upstream LLM API returns HTTP 429 after retry exhaustion."""


class LLMTimeoutError(LLMError):
    """Raised when an LLM call exceeds the configured timeout budget."""


class NeedsManualInput(LLMError):
    """Raised by `ManualProvider` when `reports/<period>/response.md` is absent."""

    def __init__(self, message: str, *, prompt_path: Path, response_path: Path) -> None:
        super().__init__(message)
        self.prompt_path = prompt_path
        self.response_path = response_path


def _is_kill_switch_env_set() -> bool:
    """Return True if AUTOPILOT_LLM_DISABLED is set to a truthy value in the environment."""
    raw = os.environ.get("AUTOPILOT_LLM_DISABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_rate_limit_exception(exc: BaseException) -> bool:
    """Detect whether an exception from the Gemini SDK represents HTTP 429 / rate limit."""
    for attr in ("code", "status_code", "http_status"):
        val = getattr(exc, attr, None)
        if val == 429 or str(val) == "429":
            return True
    msg = str(exc).upper()
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "TOO MANY REQUESTS" in msg


class LLMProvider(ABC):
    """Abstract base class for all LLM narrative generation providers."""

    @abstractmethod
    def generate(self, prompt: str, max_output_tokens: int = 2048) -> LLMResult:
        """Generate a completion for `prompt` capped at `max_output_tokens`."""


class BudgetGuard(LLMProvider):
    """Enforces per-run call limits, output token caps, timeouts, and the kill switch.

    Can be used either as a decorator wrapping an `LLMProvider` (`guard.generate(...)`)
    or directly (`guard.execute(provider, prompt, max_output_tokens)` /
    `guard.check_before_call(...)`).
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        max_calls_per_run: int = 5,
        max_output_tokens: int = 2048,
        timeout_seconds: float = 30.0,
        llm_disabled: bool | None = None,
    ) -> None:
        if max_calls_per_run <= 0:
            raise ValueError(f"max_calls_per_run must be > 0, got {max_calls_per_run}")
        if max_output_tokens <= 0:
            raise ValueError(f"max_output_tokens must be > 0, got {max_output_tokens}")
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")

        self._provider = provider
        self.max_calls_per_run = int(max_calls_per_run)
        self.max_output_tokens = int(max_output_tokens)
        self.timeout_seconds = float(timeout_seconds)
        self._llm_disabled_flag = llm_disabled

        self._calls_made: int = 0
        self._total_prompt_tokens: int = 0
        self._total_output_tokens: int = 0

    @property
    def calls_made(self) -> int:
        """Return the number of LLM calls attempted in the current run."""
        return self._calls_made

    @property
    def remaining_calls(self) -> int:
        """Return the remaining allowed LLM calls in the current run."""
        return max(0, self.max_calls_per_run - self._calls_made)

    @property
    def total_prompt_tokens(self) -> int:
        """Return cumulative prompt tokens recorded in the current run."""
        return self._total_prompt_tokens

    @property
    def total_output_tokens(self) -> int:
        """Return cumulative output tokens recorded in the current run."""
        return self._total_output_tokens

    def is_kill_switch_active(self) -> bool:
        """Return True if the kill switch is active via configuration or environment."""
        if self._llm_disabled_flag is True:
            return True
        return _is_kill_switch_env_set()

    def check_before_call(self, max_output_tokens: int) -> None:
        """Validate kill switch, remaining call budget, and output token budget."""
        if self.is_kill_switch_active():
            raise LLMDisabledError(
                "LLM execution blocked: AUTOPILOT_LLM_DISABLED kill switch is active."
            )
        if self._calls_made >= self.max_calls_per_run:
            raise LLMBudgetExceededError(
                f"LLM call budget exceeded: {self._calls_made}/{self.max_calls_per_run} "
                f"calls already used in this run."
            )
        if max_output_tokens <= 0 or max_output_tokens > self.max_output_tokens:
            raise LLMBudgetExceededError(
                f"Requested max_output_tokens ({max_output_tokens}) violates budget cap "
                f"(1..{self.max_output_tokens})."
            )

    def execute(
        self,
        provider: LLMProvider,
        prompt: str,
        max_output_tokens: int | None = None,
    ) -> LLMResult:
        """Execute a single guarded call against `provider`."""
        effective_max_tokens = (
            self.max_output_tokens if max_output_tokens is None else int(max_output_tokens)
        )
        self.check_before_call(effective_max_tokens)
        self._calls_made += 1

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(provider.generate, prompt, effective_max_tokens)
        try:
            result = future.result(timeout=self.timeout_seconds)
        except FuturesTimeoutError as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            logger.error(
                "LLM call timed out: call=%d/%d timeout_seconds=%.2f",
                self._calls_made,
                self.max_calls_per_run,
                self.timeout_seconds,
            )
            raise LLMTimeoutError(
                f"LLM generation timed out after {self.timeout_seconds:.2f}s"
            ) from exc
        finally:
            executor.shutdown(wait=False)

        if result.output_tokens > effective_max_tokens:
            raise LLMBudgetExceededError(
                f"Provider returned {result.output_tokens} output tokens, exceeding "
                f"allowed cap of {effective_max_tokens}."
            )

        self._total_prompt_tokens += result.prompt_tokens
        self._total_output_tokens += result.output_tokens

        logger.info(
            "LLM guarded call succeeded: provider=%s model=%s status=OK "
            "call=%d/%d prompt_tokens=%d output_tokens=%d total_tokens=%d",
            result.provider,
            result.model,
            self._calls_made,
            self.max_calls_per_run,
            result.prompt_tokens,
            result.output_tokens,
            result.total_tokens,
        )
        return result

    def generate(self, prompt: str, max_output_tokens: int = 2048) -> LLMResult:
        """Execute `generate` on the wrapped provider through this budget guard."""
        if self._provider is None:
            raise LLMConfigurationError("BudgetGuard has no wrapped LLMProvider instance.")
        return self.execute(self._provider, prompt, max_output_tokens=max_output_tokens)


class GeminiProvider(LLMProvider):
    """LLMProvider backed by the official Google Gemini SDK (`google-genai`).

    Invariants:
    - Exclusively reads the Gemini API key via `settings.read_gemini_api_key()`.
    - Handles HTTP 429 rate-limit responses with exponential backoff.
    - Hardcodes no quotas.
    - Logs only status and token counts; never logs prompts, responses, or keys.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        api_key = settings.read_gemini_api_key()
        if not api_key and client is None:
            raise LLMConfigurationError(
                "GEMINI_API_KEY must be set in environment variables to use GeminiProvider."
            )

        self.model = settings.llm_model
        self.max_retries = settings.llm_max_retries
        self.backoff_base_seconds = settings.llm_backoff_base_seconds
        self.timeout_seconds = settings.llm_timeout_seconds
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self._client = client if client is not None else genai.Client(api_key=api_key)

    def _compute_backoff(self, attempt: int) -> float:
        """Compute exponential backoff delay for a 0-based retry attempt."""
        return float(self.backoff_base_seconds * (2 ** max(0, attempt)))

    def generate(self, prompt: str, max_output_tokens: int = 2048) -> LLMResult:
        """Generate completion via the official `google.genai` SDK with 429 backoff."""
        if _is_kill_switch_env_set():
            raise LLMDisabledError(
                "GeminiProvider blocked: AUTOPILOT_LLM_DISABLED kill switch is active."
            )

        config = types.GenerateContentConfig(max_output_tokens=int(max_output_tokens))

        for attempt in range(self.max_retries + 1):
            start_ts = self._clock_fn()
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )
            except Exception as exc:
                elapsed_ms = (self._clock_fn() - start_ts) * 1000.0
                if _is_rate_limit_exception(exc):
                    if attempt < self.max_retries:
                        delay = self._compute_backoff(attempt)
                        logger.warning(
                            "Gemini rate limited (status=429) on attempt=%d/%d; backing off %.2fs",
                            attempt + 1,
                            self.max_retries + 1,
                            delay,
                        )
                        self._sleep_fn(delay)
                        continue
                    logger.error(
                        "Gemini rate limit persisted (status=429) after %d attempts",
                        self.max_retries + 1,
                    )
                    raise LLMRateLimitError(
                        f"Gemini API returned 429 after {self.max_retries + 1} attempts"
                    ) from exc

                logger.error(
                    "Gemini request failed: model=%s status=ERROR attempt=%d/%d elapsed_ms=%.1f",
                    self.model,
                    attempt + 1,
                    self.max_retries + 1,
                    elapsed_ms,
                )
                raise LLMError(f"Gemini generation failed: {type(exc).__name__}") from exc

            elapsed_ms = (self._clock_fn() - start_ts) * 1000.0
            usage = getattr(response, "usage_metadata", None)
            prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
            total_tokens = int(
                getattr(usage, "total_token_count", 0) or (prompt_tokens + output_tokens)
            )
            text = str(getattr(response, "text", "") or "")

            logger.info(
                "Gemini response: model=%s status=200 prompt_tokens=%d "
                "output_tokens=%d total_tokens=%d elapsed_ms=%.1f",
                self.model,
                prompt_tokens,
                output_tokens,
                total_tokens,
                elapsed_ms,
            )
            return LLMResult(
                text=text,
                provider="gemini",
                model=self.model,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=elapsed_ms,
                finish_reason="STOP",
            )

        raise LLMError("Unexpected exit from Gemini retry loop")


class ManualProvider(LLMProvider):
    """Human-in-the-loop provider exchanging markdown files in `reports/<period>/`.

    Writes the prompt to `reports/<period>/prompt.md` and reads
    `reports/<period>/response.md`, raising `NeedsManualInput` if absent.
    """

    def __init__(self, reports_dir: Path | str = Path("reports"), period: str = "2026-Q2") -> None:
        self.reports_dir = Path(reports_dir)
        self.period = period

    @property
    def prompt_path(self) -> Path:
        """Return target path for the generated prompt markdown file."""
        return self.reports_dir / self.period / "prompt.md"

    @property
    def response_path(self) -> Path:
        """Return expected path for the human-authored response markdown file."""
        return self.reports_dir / self.period / "response.md"

    def generate(self, prompt: str, max_output_tokens: int = 2048) -> LLMResult:
        """Write `prompt.md` and read `response.md`, or raise `NeedsManualInput`."""
        period_dir = self.reports_dir / self.period
        period_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_path.write_text(prompt, encoding="utf-8")

        if not self.response_path.exists():
            logger.info(
                "ManualProvider awaiting response: status=NEEDS_MANUAL_INPUT period=%s",
                self.period,
            )
            raise NeedsManualInput(
                f"Manual response file not found at '{self.response_path}'. "
                f"Prompt has been written to '{self.prompt_path}'. "
                f"Populate '{self.response_path}' and re-run the pipeline.",
                prompt_path=self.prompt_path,
                response_path=self.response_path,
            )

        response_text = self.response_path.read_text(encoding="utf-8").strip()
        if not response_text:
            raise NeedsManualInput(
                f"Manual response file at '{self.response_path}' is empty. "
                f"Review '{self.prompt_path}' and populate '{self.response_path}'.",
                prompt_path=self.prompt_path,
                response_path=self.response_path,
            )

        estimated_output_tokens = max(1, len(response_text.split()))
        if estimated_output_tokens > max_output_tokens:
            logger.warning(
                "ManualProvider response word count (%d) exceeds max_output_tokens (%d)",
                estimated_output_tokens,
                max_output_tokens,
            )

        logger.info(
            "ManualProvider loaded response: status=OK period=%s output_tokens=%d",
            self.period,
            estimated_output_tokens,
        )
        return LLMResult(
            text=response_text,
            provider="manual",
            model="human-review",
            prompt_tokens=max(1, len(prompt.split())),
            output_tokens=estimated_output_tokens,
            total_tokens=max(1, len(prompt.split())) + estimated_output_tokens,
            latency_ms=0.0,
            finish_reason="STOP",
        )


class NullProvider(LLMProvider):
    """Offline deterministic template-based provider requiring no network or keys."""

    def __init__(self, period: str = "2026-Q2", baseline_period: str = "2026-Q1") -> None:
        self.period = period
        self.baseline_period = baseline_period

    def generate(self, prompt: str, max_output_tokens: int = 2048) -> LLMResult:
        """Return a deterministic markdown research note draft."""
        draft = (
            f"# Institutional 13F Research Note ({self.period})\n\n"
            f"## Overview\n"
            f"- **Reporting Period**: {self.period}\n"
            f"- **Comparison Baseline**: {self.baseline_period}\n"
            f"- **Generation Mode**: Deterministic template (`NullProvider`)\n\n"
            f"## Quantitative Summary\n"
            f"All figures in this report are sourced directly from the verified "
            f"`FactsBundle` for {self.period}.\n\n"
            f"---\n"
            f"*Disclaimer: Not investment advice; AI-drafted, figures checked against "
            f"filings data.*\n"
        )
        prompt_tokens = max(1, len(prompt.split())) if prompt else 0
        output_tokens = min(max_output_tokens, max(1, len(draft.split())))
        logger.info(
            "NullProvider generated deterministic draft: status=OK "
            "prompt_tokens=%d output_tokens=%d",
            prompt_tokens,
            output_tokens,
        )
        return LLMResult(
            text=draft,
            provider="null",
            model="deterministic-template",
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            total_tokens=prompt_tokens + output_tokens,
            latency_ms=0.0,
            finish_reason="STOP",
        )


def select_llm_provider(
    settings: Settings,
    *,
    period: str | None = None,
    wrap_with_budget_guard: bool = False,
    client: Any = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> LLMProvider:
    """Instantiate the configured `LLMProvider` from `Settings`.

    Notice that `settings.read_gemini_api_key()` is never accessed unless
    `GeminiProvider` is actually constructed.
    """
    resolved_period = period or settings.target_period
    provider_name = settings.llm_provider.strip().lower()
    kill_switch = settings.llm_disabled or _is_kill_switch_env_set()

    if kill_switch:
        if provider_name in {"null", "auto"}:
            selected: LLMProvider = NullProvider(
                period=resolved_period,
                baseline_period=settings.baseline_period,
            )
        else:
            raise LLMDisabledError(
                f"Cannot initialize provider {provider_name!r} when "
                f"AUTOPILOT_LLM_DISABLED=1 is active."
            )
    elif provider_name == "null":
        selected = NullProvider(
            period=resolved_period,
            baseline_period=settings.baseline_period,
        )
    elif provider_name == "manual":
        selected = ManualProvider(
            reports_dir=settings.reports_dir,
            period=resolved_period,
        )
    elif provider_name == "gemini":
        selected = GeminiProvider(
            settings,
            client=client,
            sleep_fn=sleep_fn,
        )
    elif provider_name == "auto":
        if settings.has_gemini_key:
            selected = GeminiProvider(
                settings,
                client=client,
                sleep_fn=sleep_fn,
            )
        else:
            selected = NullProvider(
                period=resolved_period,
                baseline_period=settings.baseline_period,
            )
    else:
        raise LLMConfigurationError(
            f"Unsupported llm_provider {provider_name!r}; expected one of "
            f"'null', 'manual', 'gemini', or 'auto'."
        )

    if wrap_with_budget_guard:
        return BudgetGuard(
            selected,
            max_calls_per_run=settings.llm_max_calls_per_run,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
            llm_disabled=settings.llm_disabled,
        )
    return selected
