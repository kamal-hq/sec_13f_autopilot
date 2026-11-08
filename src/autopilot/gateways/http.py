"""Shared HTTP session gateway with timeouts, exponential backoff retries, and rate limiting.

Security invariant: Never log request or response bodies or credential headers.
Log only HTTP method, status code, retry attempt, and latency.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class HttpError(Exception):
    """Base typed exception for HTTP gateway errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class HttpRateLimitError(HttpError):
    """Raised when HTTP 429 Too Many Requests persists after retries."""


class HttpServerError(HttpError):
    """Raised when HTTP 5xx server error persists after retries."""


class HttpClientError(HttpError):
    """Raised on non-retryable HTTP 4xx client errors (excluding 429)."""


class HttpTimeoutError(HttpError):
    """Raised when an HTTP request times out after all retry attempts."""


class HttpConnectionError(HttpError):
    """Raised when an HTTP connection fails after all retry attempts."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Normalized HTTP response returned by `HttpClient`."""

    status_code: int
    text: str
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""
    elapsed_ms: float = 0.0

    def json(self) -> Any:
        """Deserialize the response body as JSON."""
        return json.loads(self.text)


class RateLimiter:
    """Thread-safe minimum-interval rate limiter."""

    def __init__(
        self,
        max_calls_per_second: float = 8.0,
        *,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls_per_second <= 0:
            raise ValueError(f"max_calls_per_second must be > 0, got {max_calls_per_second}")
        self._max_calls_per_second = float(max_calls_per_second)
        self._min_interval = 1.0 / self._max_calls_per_second
        self._clock_fn = clock_fn
        self._sleep_fn = sleep_fn
        self._lock = threading.Lock()
        self._last_call_ts: float | None = None

    @property
    def max_calls_per_second(self) -> float:
        """Return configured maximum requests per second."""
        return self._max_calls_per_second

    @property
    def min_interval(self) -> float:
        """Return minimum interval in seconds between consecutive calls."""
        return self._min_interval

    def acquire(self) -> float:
        """Wait until the next request slot is permitted and return sleep duration."""
        with self._lock:
            now = self._clock_fn()
            if self._last_call_ts is None:
                self._last_call_ts = now
                return 0.0

            elapsed = now - self._last_call_ts
            wait_seconds = self._min_interval - elapsed
            if wait_seconds > 0:
                self._sleep_fn(wait_seconds)
                self._last_call_ts = self._clock_fn()
                return wait_seconds

            self._last_call_ts = now
            return 0.0


class HttpClient:
    """Shared HTTP session with timeout enforcement, rate limiting, and exponential backoff."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 30.0,
        backoff_factor: float = 2.0,
        rate_limiter: RateLimiter | None = None,
        default_headers: dict[str, str] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
        retry_statuses: frozenset[int] = RETRYABLE_STATUS_CODES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")

        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.backoff_base_seconds = float(backoff_base_seconds)
        self.backoff_max_seconds = float(backoff_max_seconds)
        self.backoff_factor = float(backoff_factor)
        self.rate_limiter = rate_limiter
        self._default_headers: dict[str, str] = dict(default_headers or {})
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self.retry_statuses = retry_statuses

    def _compute_backoff(self, attempt: int, headers: dict[str, str] | None = None) -> float:
        """Compute backoff delay in seconds for the given 0-based retry attempt."""
        if headers:
            for key, raw_val in headers.items():
                if key.lower() == "retry-after":
                    try:
                        parsed = float(raw_val.strip())
                        if parsed >= 0:
                            return min(parsed, self.backoff_max_seconds)
                    except ValueError:
                        pass
        delay = self.backoff_base_seconds * (self.backoff_factor ** max(0, attempt))
        return min(delay, self.backoff_max_seconds)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """Perform an HTTP request with rate limiting, timeout, and exponential backoff."""
        effective_timeout = self.timeout_seconds if timeout is None else float(timeout)
        merged_headers = {**self._default_headers, **(headers or {})}

        for attempt in range(self.max_retries + 1):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()

            start_ts = self._clock_fn()
            try:
                raw_resp = self._session.request(
                    method=method.upper(),
                    url=url,
                    headers=merged_headers,
                    params=params,
                    timeout=effective_timeout,
                )
            except requests.Timeout as exc:
                elapsed_ms = (self._clock_fn() - start_ts) * 1000.0
                logger.warning(
                    "HTTP timeout: method=%s attempt=%d/%d elapsed_ms=%.1f",
                    method.upper(),
                    attempt + 1,
                    self.max_retries + 1,
                    elapsed_ms,
                )
                if attempt < self.max_retries:
                    self._sleep_fn(self._compute_backoff(attempt))
                    continue
                raise HttpTimeoutError(
                    f"Request timed out after {self.max_retries + 1} attempts",
                    url=url,
                ) from exc
            except requests.RequestException as exc:
                elapsed_ms = (self._clock_fn() - start_ts) * 1000.0
                logger.warning(
                    "HTTP connection error: method=%s attempt=%d/%d elapsed_ms=%.1f",
                    method.upper(),
                    attempt + 1,
                    self.max_retries + 1,
                    elapsed_ms,
                )
                if attempt < self.max_retries:
                    self._sleep_fn(self._compute_backoff(attempt))
                    continue
                raise HttpConnectionError(
                    f"Request failed after {self.max_retries + 1} attempts",
                    url=url,
                ) from exc

            elapsed_ms = (self._clock_fn() - start_ts) * 1000.0
            status = int(raw_resp.status_code)
            resp_headers = dict(raw_resp.headers or {})

            logger.info(
                "HTTP response: method=%s status=%d attempt=%d/%d elapsed_ms=%.1f",
                method.upper(),
                status,
                attempt + 1,
                self.max_retries + 1,
                elapsed_ms,
            )

            if status in self.retry_statuses:
                if attempt < self.max_retries:
                    delay = self._compute_backoff(attempt, resp_headers)
                    logger.warning(
                        "HTTP retryable status=%d on attempt=%d/%d; backing off %.2fs",
                        status,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    self._sleep_fn(delay)
                    continue
                if status == 429:
                    raise HttpRateLimitError(
                        f"HTTP 429 rate limit exceeded after {self.max_retries + 1} attempts",
                        status_code=429,
                        url=url,
                    )
                raise HttpServerError(
                    f"HTTP {status} server error after {self.max_retries + 1} attempts",
                    status_code=status,
                    url=url,
                )

            if 400 <= status < 500:
                raise HttpClientError(
                    f"HTTP {status} client error",
                    status_code=status,
                    url=url,
                )

            if status >= 500:
                raise HttpServerError(
                    f"HTTP {status} server error",
                    status_code=status,
                    url=url,
                )

            content_bytes = raw_resp.content if isinstance(raw_resp.content, bytes) else b""
            text_val = raw_resp.text if isinstance(raw_resp.text, str) else ""
            return HttpResponse(
                status_code=status,
                text=text_val,
                content=content_bytes,
                headers=resp_headers,
                url=url,
                elapsed_ms=elapsed_ms,
            )

        raise HttpError("Unexpected exit from HTTP retry loop", url=url)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """Send an HTTP GET request."""
        return self.request("GET", url, headers=headers, params=params, timeout=timeout)

    def close(self) -> None:
        """Close the underlying requests session if owned by this client."""
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
