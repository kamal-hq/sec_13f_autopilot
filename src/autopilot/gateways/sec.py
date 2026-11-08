"""SEC EDGAR gateway enforcing User-Agent from Settings, <=8 req/s rate limit, and backoff."""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from autopilot.models import Filing
from autopilot.settings import Settings

logger = logging.getLogger(__name__)

MAX_SEC_REQUESTS_PER_SECOND: float = 8.0
COVER_NS: dict[str, str] = {"n": "http://www.sec.gov/edgar/thirteenffiler"}

_PLACEHOLDER_USER_AGENTS = frozenset(
    {
        "",
        "your name your.email@example.com",
        "your name your@email.com",
        "sec_13f_analyzer research@example.com",
    }
)

_SUBMISSIONS_URL_RE = re.compile(r"/submissions/CIK(\d{10})\.json$")
_ARCHIVE_URL_RE = re.compile(r"/Archives/edgar/data/(\d+)/(\d{18})/([^/?#]+)$")


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


def _format_accession_with_dashes(acc_no_dashes: str) -> str:
    """Convert an 18-digit hyphenless accession string into `XXXXXXXXXX-YY-ZZZZZZ`."""
    clean = acc_no_dashes.strip()
    if len(clean) == 18 and clean.isdigit():
        return f"{clean[:10]}-{clean[10:12]}-{clean[12:]}"
    return clean


class _SimpleFixtureResponse:
    """Lightweight `requests.Response`-compatible object returned by `FixtureSecSession`."""

    def __init__(
        self,
        status_code: int,
        text: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {"Content-Type": "application/json"}


class FixtureSecSession:
    """Offline `requests.Session` replacement serving recorded SEC EDGAR fixtures."""

    def __init__(
        self,
        fixtures_dir: Path | str = Path("tests/fixtures"),
        *,
        raw_xml_dirs: list[Path] | None = None,
    ) -> None:
        self.fixtures_dir = Path(fixtures_dir)
        self.submissions_dir = (
            self.fixtures_dir / "submissions"
            if (self.fixtures_dir / "submissions").exists()
            else self.fixtures_dir
        )
        cover_file = self.fixtures_dir / "cover_pages.json"
        if not cover_file.exists():
            cover_file = self.submissions_dir / "cover_pages.json"
        self.cover_pages: dict[str, dict[str, Any]] = (
            json.loads(cover_file.read_text(encoding="utf-8")) if cover_file.exists() else {}
        )
        self.raw_xml_dirs = list(
            raw_xml_dirs
            or [
                Path("data/raw"),
                self.fixtures_dir / "raw",
            ]
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _SimpleFixtureResponse:
        """Serve recorded SEC submissions JSON, index.json, primary_doc.xml, or info table XML."""
        _ = (method, headers, params, timeout)

        sub_match = _SUBMISSIONS_URL_RE.search(url)
        if sub_match:
            cik = sub_match.group(1)
            sub_path = self.submissions_dir / f"CIK{cik}.json"
            if not sub_path.exists():
                return _SimpleFixtureResponse(404, '{"error": "Not Found"}')
            return _SimpleFixtureResponse(200, sub_path.read_text(encoding="utf-8"))

        arch_match = _ARCHIVE_URL_RE.search(url)
        if arch_match:
            cik_int, acc_no_dashes, doc_name = arch_match.groups()
            padded_cik = cik_int.zfill(10)
            accession = _format_accession_with_dashes(acc_no_dashes)
            meta = self.cover_pages.get(accession, {})
            primary_doc = str(meta.get("primary_doc", "primary_doc.xml"))
            infotable_doc = str(meta.get("infotable_doc", "form13fInfoTable.xml"))

            if doc_name == "index.json":
                index_payload = {
                    "directory": {
                        "item": [
                            {"name": primary_doc},
                            {"name": infotable_doc},
                        ]
                    }
                }
                return _SimpleFixtureResponse(200, json.dumps(index_payload))

            if doc_name == primary_doc or "primary_doc" in doc_name.lower():
                is_amd = "true" if meta.get("is_amendment") else "false"
                amd_type = meta.get("amendment_type") or ""
                entry_tot = meta.get("table_entry_total")
                val_tot = meta.get("table_value_total")
                amd_block = f"<isAmendment>{is_amd}</isAmendment>" + (
                    f"<amendmentType>{amd_type}</amendmentType>" if amd_type else ""
                )
                entry_val = entry_tot if entry_tot is not None else 0
                total_val = val_tot if val_tot is not None else 0
                xml_body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">'
                    f"<headerData><filerInfo><filer><credentials><cik>{padded_cik}</cik>"
                    "</credentials></filer></filerInfo></headerData>"
                    f"<formData><coverPage><amendmentInfo>{amd_block}</amendmentInfo></coverPage>"
                    "<summaryPage>"
                    f"<tableEntryTotal>{entry_val}</tableEntryTotal>"
                    f"<tableValueTotal>{total_val}</tableValueTotal>"
                    "</summaryPage></formData></edgarSubmission>"
                )
                return _SimpleFixtureResponse(
                    200,
                    xml_body,
                    headers={"Content-Type": "application/xml"},
                )

            # Information table XML request
            for raw_dir in self.raw_xml_dirs:
                candidate = raw_dir / f"{padded_cik}_{accession}.xml"
                if candidate.exists():
                    return _SimpleFixtureResponse(
                        200,
                        candidate.read_text(encoding="utf-8"),
                        headers={"Content-Type": "application/xml"},
                    )

        return _SimpleFixtureResponse(404, '{"error": "Fixture resource not found"}')

    def close(self) -> None:
        """No-op session close."""


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
        session: Any = None,
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

    @classmethod
    def from_fixtures(
        cls,
        settings: Settings | None = None,
        *,
        fixtures_dir: Path | str = Path("tests/fixtures"),
        raw_xml_dirs: list[Path] | None = None,
    ) -> SecGateway:
        """Create an offline `SecGateway` backed by `FixtureSecSession`."""
        base = settings or Settings(sec_user_agent="AutopilotReplay replay@local.test")
        if not base.sec_user_agent or "@" not in base.sec_user_agent:
            base.sec_user_agent = "AutopilotReplay replay@local.test"
        fixture_session = FixtureSecSession(fixtures_dir, raw_xml_dirs=raw_xml_dirs)
        return cls(
            base,
            session=fixture_session,
            sleep_fn=lambda _: None,
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

    def inspect_filing_cover_page(
        self,
        cik: str,
        accession_number: str,
        *,
        primary_document: str | None = None,
    ) -> dict[str, Any]:
        """Fetch `index.json` and `primary_doc.xml` via `SecGateway` for cover-page metadata."""
        numeric_cik = str(int(cik.strip()))
        acc_no_dashes = accession_number.strip().replace("-", "")
        base_archive = f"{self._settings.sec_edgar_archives_url}/{numeric_cik}/{acc_no_dashes}"

        index_data = self.get_json(f"{base_archive}/index.json")
        items = index_data.get("directory", {}).get("item", []) or []
        xml_files = [
            str(item.get("name", ""))
            for item in items
            if str(item.get("name", "")).lower().endswith(".xml")
        ]
        raw_primary_from_index = next(
            (f for f in xml_files if "primary_doc" in f.lower() and "/" not in f),
            None,
        )
        if raw_primary_from_index:
            resolved_primary = raw_primary_from_index
        elif primary_document:
            resolved_primary = primary_document.rsplit("/", 1)[-1]
        else:
            resolved_primary = "primary_doc.xml"
        infotable_doc = next(
            (f for f in xml_files if "primary_doc" not in f.lower()),
            "",
        )

        info: dict[str, Any] = {
            "primary_doc": resolved_primary,
            "infotable_doc": infotable_doc,
            "is_amendment": False,
            "amendment_type": None,
            "table_entry_total": None,
            "table_value_total": None,
        }

        if resolved_primary:
            cover_resp = self.get(f"{base_archive}/{resolved_primary.lstrip('/')}")
            try:
                root = ET.fromstring(cover_resp.text)
                is_amd_str = (
                    root.findtext(".//n:isAmendment", default="false", namespaces=COVER_NS)
                    .strip()
                    .lower()
                )
                amd_type_str = (
                    root.findtext(".//n:amendmentType", default="", namespaces=COVER_NS)
                    .strip()
                    .upper()
                )
                entry_str = root.findtext(
                    ".//n:tableEntryTotal", default="", namespaces=COVER_NS
                ).strip()
                val_str = root.findtext(
                    ".//n:tableValueTotal", default="", namespaces=COVER_NS
                ).strip()

                info["is_amendment"] = is_amd_str == "true"
                info["amendment_type"] = amd_type_str or None
                info["table_entry_total"] = int(entry_str) if entry_str.isdigit() else None
                info["table_value_total"] = int(val_str) if val_str.isdigit() else None
            except ET.ParseError as exc:
                logger.warning(
                    "Could not parse cover page XML for %s / %s: %s",
                    cik,
                    accession_number,
                    exc,
                )
        return info

    def get_latest_13f_metadata(
        self,
        cik: str,
        *,
        fund_name: str = "",
        forms: tuple[str, ...] = ("13F-HR", "13F-HR/A"),
        as_of: str | None = None,
        min_period: str | None = None,
        inspect_cover_pages: bool = False,
    ) -> list[Filing]:
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
        amendment_types: list[str | None] = recent.get("amendmentType", []) or []
        table_values: list[int | None] = recent.get("tableValueTotal", []) or []
        table_entries: list[int | None] = recent.get("tableEntryTotal", []) or []
        infotable_docs: list[str] = recent.get("infotableDocument", []) or []

        results: list[Filing] = []
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
            filed_date = filing_dates[idx]
            if as_of is not None and filed_date > as_of:
                continue
            report_date = report_dates[idx]
            period = _report_date_to_period(report_date)
            if min_period is not None and period < min_period:
                continue

            primary_doc = primary_docs[idx] if idx < len(primary_docs) else "primary_doc.xml"
            amd_type = amendment_types[idx] if idx < len(amendment_types) else None
            val_tot = table_values[idx] if idx < len(table_values) else None
            entry_tot = table_entries[idx] if idx < len(table_entries) else None
            info_doc = infotable_docs[idx] if idx < len(infotable_docs) else ""

            if inspect_cover_pages and (
                val_tot is None or (form_type == "13F-HR/A" and amd_type is None)
            ):
                cover = self.inspect_filing_cover_page(
                    padded_cik,
                    accession_list[idx],
                    primary_document=primary_doc,
                )
                amd_type = cover["amendment_type"]
                val_tot = cover["table_value_total"]
                entry_tot = cover["table_entry_total"]
                info_doc = str(cover["infotable_doc"] or "")

            results.append(
                Filing(
                    cik=padded_cik,
                    fund_name=resolved_name,
                    accession=accession_list[idx],
                    form_type=form_type,
                    filed_date=filed_date,
                    period=period,
                    amendment_type=amd_type,
                    report_date=report_date,
                    primary_document=primary_doc,
                    infotable_document=info_doc,
                    table_entry_total=entry_tot,
                    table_value_total=val_tot,
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

    def fetch_information_table_xml(
        self,
        cik: str,
        accession_number: str,
        infotable_doc: str | None = None,
    ) -> str:
        """Download the 13F information table XML document via `SecGateway`."""
        resolved_doc = infotable_doc
        if not resolved_doc:
            cover = self.inspect_filing_cover_page(cik, accession_number)
            resolved_doc = str(cover.get("infotable_doc") or "")
        if not resolved_doc:
            raise SecNotFoundError(
                f"No information table XML document found for {cik} / {accession_number}",
                status_code=404,
            )
        return self.fetch_filing_document(cik, accession_number, resolved_doc)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()
