"""SEC EDGAR gateway enforcing User-Agent from Settings, <=8 req/s rate limit, and backoff."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from autopilot.gateways.http import (
    HttpClient,
    HttpClientError,
    HttpConnectionError,
    HttpError,
    HttpRateLimitError,
    HttpResponse,
    HttpServerError,
    HttpTimeoutError,
    RateLimiter,
)
from autopilot.models import SecFilingMetadata
from autopilot.settings import Settings

logger = logging.getLogger(__name__)

MAX_SEC_REQUESTS_PER_SECOND: float = 8.0

_PLACEHOLDER_USER_AGENTS = frozenset(
    {
        "",
        "your name your.email@example.com",
        "your name your@email.com",
        "sec_13f_analyzer research@example.com",
    }
)


class SecError(HttpError):
    """Base typed error for all SEC EDGAR gateway operations."""


class SecConfigurationError(SecError):
    """Raised when SEC_USER_AGENT is missing, placeholder, or invalid."""


class SecRateLimitError(SecError, HttpRateLimitError):
    """Raised when SEC EDGAR returns HTTP 429 after retry exhaustion."""


class SecServerError(SecError, HttpServerError):
    """Raised when SEC EDGAR returns HTTP 5xx after retry exhaustion."""


class SecClientError(SecError, HttpClientError):
    """Raised when SEC EDGAR returns a non-retryable HTTP 4xx client error."""


class SecNotFoundError(SecClientError):
    """Raised when an SEC EDGAR resource returns HTTP 404 Not Found."""


class SecNetworkError(SecError):
    """Raised when an SEC EDGAR request fails due to timeout or connection error."""


def _report_date_to_period(report_date: str) -> str:
    """Convert an ISO report date (`YYYY-MM-DD`) into a quarter label (`YYYY-QN`)."""
    parts = report_date.strip().split("-")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return report_date
    year = parts[0]
    month = int(parts[1])
    quarter = (month - 1) // 3 + 1
    return f"{year}-Q{quarter}"


class SecGateway:
    """Gateway for SEC EDGAR submissions and Form 13F filings.

    Invariants:
    - `User-Agent` header comes exclusively from `Settings.sec_user_agent`.
    - Rate limited to at most 8 requests/second (`MAX_SEC_REQUESTS_PER_SECOND`).
    - Exponential backoff on HTTP 429 and 5xx (500, 502, 503, 504).
    - Raises typed `SecError` subclasses on failure.
    """

    MAX_REQUESTS_PER_SECOND: float = MAX_SEC_REQUESTS_PER_SECOND

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: HttpClient | None = None,
        session: requests.Session | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        user_agent = settings.sec_user_agent.strip()
        if user_agent.lower() in _PLACEHOLDER_USER_AGENTS or "@" not in user_agent:
            raise SecConfigurationError(
                "Valid SEC_USER_AGENT ('Name email@domain.com') must be set in environment."
            )

        configured_rps = float(settings.sec_max_requests_per_second)
        if configured_rps <= 0:
            raise SecConfigurationError(
                f"sec_max_requests_per_second must be > 0, got {configured_rps}"
            )

        self._settings = settings
        self._user_agent = user_agent
        self.max_requests_per_second = min(configured_rps, self.MAX_REQUESTS_PER_SECOND)
        self.rate_limiter = RateLimiter(
            max_calls_per_second=self.max_requests_per_second,
            clock_fn=clock_fn,
            sleep_fn=sleep_fn,
        )

        default_headers = {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, application/xml, text/xml, */*",
        }

        self._http = (
            http_client
            if http_client is not None
            else HttpClient(
                session=session,
                timeout_seconds=settings.sec_timeout_seconds,
                max_retries=settings.sec_max_retries,
                backoff_base_seconds=settings.sec_backoff_base_seconds,
                backoff_max_seconds=settings.sec_backoff_max_seconds,
                rate_limiter=self.rate_limiter,
                default_headers=default_headers,
                sleep_fn=sleep_fn,
                clock_fn=clock_fn,
            )
        )

    @property
    def user_agent(self) -> str:
        """Return the active User-Agent string configured for SEC requests."""
        return self._user_agent

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> HttpResponse:
        """Perform a rate-limited GET request to SEC EDGAR with typed error translation."""
        headers = {"User-Agent": self._user_agent}
        try:
            return self._http.get(url, headers=headers, params=params)
        except HttpRateLimitError as exc:
            raise SecRateLimitError(
                str(exc),
                status_code=exc.status_code or 429,
                url=url,
            ) from exc
        except HttpServerError as exc:
            raise SecServerError(
                str(exc),
                status_code=exc.status_code,
                url=url,
            ) from exc
        except HttpClientError as exc:
            if exc.status_code == 404:
                raise SecNotFoundError(
                    f"SEC resource not found (HTTP 404): {url}",
                    status_code=404,
                    url=url,
                ) from exc
            raise SecClientError(
                str(exc),
                status_code=exc.status_code,
                url=url,
            ) from exc
        except (HttpTimeoutError, HttpConnectionError) as exc:
            raise SecNetworkError(
                str(exc),
                status_code=exc.status_code,
                url=url,
            ) from exc

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch and decode a JSON payload from SEC EDGAR."""
        response = self.get(url, params=params)
        try:
            data = response.json()
        except ValueError as exc:
            raise SecError(
                "Invalid JSON payload returned by SEC EDGAR",
                status_code=response.status_code,
                url=url,
            ) from exc
        if not isinstance(data, dict):
            raise SecError(
                f"Expected JSON object from SEC EDGAR, got {type(data).__name__}",
                status_code=response.status_code,
                url=url,
            )
        return data

    def get_submissions(self, cik: str) -> dict[str, Any]:
        """Fetch SEC EDGAR submissions metadata JSON for a 10-digit CIK."""
        padded_cik = cik.strip().zfill(10)
        url = f"{self._settings.sec_base_url}/submissions/CIK{padded_cik}.json"
        return self.get_json(url)

    def get_latest_13f_metadata(
        self,
        cik: str,
        *,
        fund_name: str = "",
        forms: tuple[str, ...] = ("13F-HR", "13F-HR/A"),
    ) -> list[SecFilingMetadata]:
        """Extract Form 13F filing metadata entries from a fund's submissions payload."""
        padded_cik = cik.strip().zfill(10)
        payload = self.get_submissions(padded_cik)
        resolved_name = fund_name or str(payload.get("name", padded_cik))
        recent = payload.get("filings", {}).get("recent", {})

        accession_list: list[str] = recent.get("accessionNumber", []) or []
        form_list: list[str] = recent.get("form", []) or []
        filing_dates: list[str] = recent.get("filingDate", []) or []
        report_dates: list[str] = recent.get("reportDate", []) or []
        primary_docs: list[str] = recent.get("primaryDocument", []) or []

        results: list[SecFilingMetadata] = []
        row_count = min(
            len(accession_list),
            len(form_list),
            len(filing_dates),
            len(report_dates),
        )
        for idx in range(row_count):
            form_type = form_list[idx]
            if form_type not in forms:
                continue
            report_date = report_dates[idx]
            primary_doc = primary_docs[idx] if idx < len(primary_docs) else "primary_doc.xml"
            results.append(
                SecFilingMetadata(
                    cik=padded_cik,
                    fund_name=resolved_name,
                    accession_number=accession_list[idx],
                    form_type=form_type,
                    filing_date=filing_dates[idx],
                    report_date=report_date,
                    period=_report_date_to_period(report_date),
                    primary_document=primary_doc,
                )
            )
        return results

    def fetch_filing_document(
        self,
        cik: str,
        accession_number: str,
        document_name: str = "primary_doc.xml",
    ) -> str:
        """Download a raw XML/text filing document from SEC EDGAR Archives."""
        numeric_cik = str(int(cik.strip()))
        acc_no_dashes = accession_number.strip().replace("-", "")
        url = (
            f"{self._settings.sec_edgar_archives_url}/"
            f"{numeric_cik}/{acc_no_dashes}/{document_name.lstrip('/')}"
        )
        response = self.get(url)
        return response.text

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()
