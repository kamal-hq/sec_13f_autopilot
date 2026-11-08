"""Offline unit tests for SecGateway rate limiting, retry/backoff, and typed errors."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from autopilot.gateways.sec import (
    SecConfigurationError,
    SecGateway,
    SecNetworkError,
    SecNotFoundError,
    SecRateLimitError,
    SecServerError,
)
from autopilot.settings import Settings


def _make_mock_response(
    status_code: int = 200,
    text: str = "{}",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock `requests.Response` object for offline testing."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = text.encode("utf-8")
    resp.headers = headers or {}
    return resp


def test_sec_gateway_requires_valid_user_agent() -> None:
    """SecGateway must reject empty or placeholder User-Agent strings."""
    with pytest.raises(SecConfigurationError):
        SecGateway(Settings(sec_user_agent=""))

    with pytest.raises(SecConfigurationError):
        SecGateway(Settings(sec_user_agent="Your Name your.email@example.com"))


def test_sec_gateway_enforces_max_8_requests_per_second() -> None:
    """SecGateway must cap requests per second at 8.0 and space consecutive calls."""
    current_time = 100.0
    recorded_sleeps: list[float] = []

    def fake_clock() -> float:
        return current_time

    def fake_sleep(seconds: float) -> None:
        nonlocal current_time
        recorded_sleeps.append(seconds)
        current_time += seconds

    settings = Settings(
        sec_user_agent="Test Researcher test@institution.org",
        sec_max_requests_per_second=25.0,  # Above 8.0 ceiling
    )
    session = MagicMock(spec=requests.Session)
    session.request.return_value = _make_mock_response(200, '{"name": "Berkshire"}')

    gateway = SecGateway(
        settings,
        session=session,
        sleep_fn=fake_sleep,
        clock_fn=fake_clock,
    )

    assert gateway.max_requests_per_second == 8.0
    assert gateway.rate_limiter.min_interval == pytest.approx(0.125)

    gateway.get("https://data.sec.gov/submissions/CIK0001067983.json")
    gateway.get("https://data.sec.gov/submissions/CIK0001067983.json")

    assert len(recorded_sleeps) == 1
    assert recorded_sleeps[0] == pytest.approx(0.125)

    # Verify User-Agent header was sent from Settings
    call_kwargs = session.request.call_args.kwargs
    assert call_kwargs["headers"]["User-Agent"] == "Test Researcher test@institution.org"


def test_sec_gateway_retries_on_429_and_5xx_with_exponential_backoff() -> None:
    """SecGateway backs off exponentially on 429 and 503 before succeeding."""
    backoff_sleeps: list[float] = []
    clock_val = 0.0

    def fake_clock() -> float:
        nonlocal clock_val
        clock_val += 1.0  # Advance enough to avoid rate-limiter sleep
        return clock_val

    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        _make_mock_response(429, "Too Many Requests"),
        _make_mock_response(503, "Service Unavailable"),
        _make_mock_response(200, '{"status": "ok"}'),
    ]

    settings = Settings(
        sec_user_agent="Test Researcher test@institution.org",
        sec_max_retries=3,
        sec_backoff_base_seconds=0.5,
    )
    gateway = SecGateway(
        settings,
        session=session,
        sleep_fn=backoff_sleeps.append,
        clock_fn=fake_clock,
    )

    data = gateway.get_json("https://data.sec.gov/submissions/CIK0001067983.json")
    assert data == {"status": "ok"}
    assert session.request.call_count == 3
    assert backoff_sleeps == [pytest.approx(0.5), pytest.approx(1.0)]


def test_sec_gateway_raises_typed_errors_when_retries_exhausted() -> None:
    """SecGateway raises SecRateLimitError, SecServerError, SecNotFoundError, SecNetworkError."""
    clock_val = 0.0

    def fake_clock() -> float:
        nonlocal clock_val
        clock_val += 1.0
        return clock_val

    settings = Settings(
        sec_user_agent="Test Researcher test@institution.org",
        sec_max_retries=2,
        sec_backoff_base_seconds=0.25,
    )

    # 1. Persistent 429 -> SecRateLimitError
    session_429 = MagicMock(spec=requests.Session)
    session_429.request.return_value = _make_mock_response(429, "Rate limited")
    gw_429 = SecGateway(
        settings,
        session=session_429,
        sleep_fn=lambda _: None,
        clock_fn=fake_clock,
    )
    with pytest.raises(SecRateLimitError) as exc_429:
        gw_429.get("https://data.sec.gov/submissions/CIK0001067983.json")
    assert exc_429.value.status_code == 429
    assert session_429.request.call_count == 3

    # 2. Persistent 502 -> SecServerError
    session_502 = MagicMock(spec=requests.Session)
    session_502.request.return_value = _make_mock_response(502, "Bad Gateway")
    gw_502 = SecGateway(
        settings,
        session=session_502,
        sleep_fn=lambda _: None,
        clock_fn=fake_clock,
    )
    with pytest.raises(SecServerError) as exc_502:
        gw_502.get("https://data.sec.gov/submissions/CIK0001067983.json")
    assert exc_502.value.status_code == 502

    # 3. HTTP 404 -> SecNotFoundError (no retry on 404)
    session_404 = MagicMock(spec=requests.Session)
    session_404.request.return_value = _make_mock_response(404, "Not Found")
    gw_404 = SecGateway(
        settings,
        session=session_404,
        sleep_fn=lambda _: None,
        clock_fn=fake_clock,
    )
    with pytest.raises(SecNotFoundError):
        gw_404.get("https://data.sec.gov/submissions/CIK0000000000.json")
    assert session_404.request.call_count == 1

    # 4. Timeout -> SecNetworkError
    session_timeout = MagicMock(spec=requests.Session)
    session_timeout.request.side_effect = requests.Timeout("Connection timed out")
    gw_timeout = SecGateway(
        settings,
        session=session_timeout,
        sleep_fn=lambda _: None,
        clock_fn=fake_clock,
    )
    with pytest.raises(SecNetworkError):
        gw_timeout.get("https://data.sec.gov/submissions/CIK0001067983.json")


def test_sec_gateway_parses_latest_13f_metadata() -> None:
    """get_latest_13f_metadata filters 13F forms and maps reportDate to quarter period."""
    payload: dict[str, Any] = {
        "name": "BERKSHIRE HATHAWAY INC",
        "filings": {
            "recent": {
                "accessionNumber": ["0001193125-26-352200", "0001193125-26-100000"],
                "form": ["13F-HR", "10-Q"],
                "filingDate": ["2026-08-14", "2026-08-05"],
                "reportDate": ["2026-06-30", "2026-06-30"],
                "primaryDocument": ["primary_doc.xml", "form10q.htm"],
            }
        },
    }
    import json

    session = MagicMock(spec=requests.Session)
    session.request.return_value = _make_mock_response(200, json.dumps(payload))
    gateway = SecGateway(
        Settings(sec_user_agent="Test Researcher test@institution.org"),
        session=session,
        sleep_fn=lambda _: None,
    )

    filings = gateway.get_latest_13f_metadata("1067983", fund_name="Berkshire Hathaway")
    assert len(filings) == 1
    assert filings[0].cik == "0001067983"
    assert filings[0].fund_name == "Berkshire Hathaway"
    assert filings[0].accession_number == "0001193125-26-352200"
    assert filings[0].period == "2026-Q2"
