"""Security, validation, and operational policy helpers for sec_13f_autopilot."""

from __future__ import annotations

import re
from dataclasses import dataclass

from autopilot.settings import REDACTED_MASK, SecretRedactionFilter, redact_text

__all__ = [
    "MAX_SEC_REQUESTS_PER_SECOND",
    "REDACTED_MASK",
    "RetryPolicy",
    "SecretRedactionFilter",
    "extract_numbers",
    "redact_text",
]

MAX_SEC_REQUESTS_PER_SECOND: float = 8.0

_NUMBER_TOKEN_RE = re.compile(r"\b\d+(?:,\d{3})*(?:\.\d+)?%?\b")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry policy configuration for HTTP and stage validation loops."""

    max_retries: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    backoff_factor: float = 2.0

    def compute_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Compute backoff delay in seconds for the given 0-based retry attempt."""
        if retry_after is not None and retry_after >= 0:
            return min(float(retry_after), self.max_delay_seconds)
        delay = self.base_delay_seconds * (self.backoff_factor ** max(0, attempt))
        return min(delay, self.max_delay_seconds)


def extract_numbers(text: str) -> set[str]:
    """Extract normalized numeric tokens from markdown text for fact-checking."""
    return {match.group(0).replace(",", "") for match in _NUMBER_TOKEN_RE.finditer(text)}
