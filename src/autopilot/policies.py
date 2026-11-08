"""Security, completeness, validation, and operational policy helpers for sec_13f_autopilot."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from autopilot.models import Filing, Period, PeriodStatus
from autopilot.settings import REDACTED_MASK, SecretRedactionFilter, redact_text

__all__ = [
    "MAX_SEC_REQUESTS_PER_SECOND",
    "REDACTED_MASK",
    "PeriodCompletenessPolicy",
    "RetryPolicy",
    "SecretRedactionFilter",
    "compute_previous_period",
    "extract_numbers",
    "redact_text",
]

MAX_SEC_REQUESTS_PER_SECOND: float = 8.0

_NUMBER_TOKEN_RE = re.compile(r"\b\d+(?:,\d{3})*(?:\.\d+)?%?\b")
_PERIOD_RE = re.compile(r"^(\d{4})-Q([1-4])$")


def compute_previous_period(period: str) -> str:
    """Return the immediately preceding quarter ('YYYY-QN') for a given `period`."""
    match = _PERIOD_RE.match(period.strip())
    if not match:
        raise ValueError(f"Invalid period format {period!r}; expected 'YYYY-QN'")
    year = int(match.group(1))
    quarter = int(match.group(2))
    if quarter == 1:
        return f"{year - 1}-Q4"
    return f"{year}-Q{quarter - 1}"


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


@dataclass(frozen=True, slots=True)
class PeriodCompletenessPolicy:
    """Evaluates filing completeness, amendment/restatement transitions, and review gates.

    Rules:
    - A period is `COMPLETE` when every configured fund has filed (and has unprocessed filings).
    - `config.yaml` supports `min_funds` and `allow_partial` for partial period readiness.
    - Amendments (`13F-HR/A`):
      - `RESTATEMENT` supersedes earlier filings for the same `(fund, period)`. If the
        superseded filing (or period) was already `PROCESSED`, the period becomes `RESTATED`
        and requires a re-run with a changelog entry.
      - `NEW HOLDINGS` sets `NEEDS_REVIEW` and is never auto-processed.
    """

    configured_funds: dict[str, str]
    min_funds: int = 7
    allow_partial: bool = False

    def evaluate_period(
        self,
        period: str,
        candidate_filings: Sequence[Filing],
        processed_accessions: set[str],
    ) -> Period:
        """Evaluate all filings for `period` against `processed_accessions` and policy rules."""
        # Group filings by padded 10-digit CIK
        by_cik: dict[str, list[Filing]] = {}
        for filing in candidate_filings:
            if filing.period != period:
                continue
            padded = filing.cik.strip().zfill(10)
            by_cik.setdefault(padded, []).append(filing)

        active_filings: list[Filing] = []
        new_filings: list[Filing] = []
        already_processed: list[str] = []
        missing_funds: list[str] = []
        superseded_accessions: dict[str, list[str]] = {}
        changelog: list[str] = []
        review_reasons: list[str] = []
        restated_processed = False

        for fund_name, raw_cik in self.configured_funds.items():
            cik = raw_cik.strip().zfill(10)
            fund_filings = sorted(
                by_cik.get(cik, []),
                key=lambda f: (f.filed_date, f.accession),
            )
            if not fund_filings:
                missing_funds.append(fund_name)
                continue

            # Check for NEW HOLDINGS (or non-RESTATEMENT) 13F-HR/A amendments
            new_holdings_amds = [
                f
                for f in fund_filings
                if f.form_type == "13F-HR/A" and (f.amendment_type or "").upper() != "RESTATEMENT"
            ]
            for amd in new_holdings_amds:
                if amd.accession not in processed_accessions:
                    amd_label = amd.amendment_type or "UNKNOWN"
                    review_reasons.append(
                        f"{fund_name} ({cik}) filed 13F-HR/A ({amd_label}) "
                        f"{amd.accession} on {amd.filed_date}; manual review required."
                    )

            # Check for RESTATEMENT 13F-HR/A amendments
            restatements = [
                f
                for f in fund_filings
                if f.form_type == "13F-HR/A" and (f.amendment_type or "").upper() == "RESTATEMENT"
            ]
            if restatements:
                latest_restatement = restatements[-1]
                superseded = [
                    f.accession for f in fund_filings if f.accession != latest_restatement.accession
                ]
                resolved_filing = replace(
                    latest_restatement,
                    fund_name=fund_name,
                    supersedes=tuple(superseded),
                )
                active_filings.append(resolved_filing)
                if superseded:
                    superseded_accessions[resolved_filing.accession] = superseded
                    changelog.append(
                        f"{fund_name} ({cik}): 13F-HR/A RESTATEMENT {resolved_filing.accession} "
                        f"(filed {resolved_filing.filed_date}) supersedes {', '.join(superseded)}"
                    )
                if resolved_filing.accession in processed_accessions:
                    already_processed.append(resolved_filing.accession)
                else:
                    new_filings.append(resolved_filing)
                    if any(old_acc in processed_accessions for old_acc in superseded):
                        restated_processed = True
            else:
                # Pick latest filing (or include NEW HOLDINGS amendment for visibility)
                latest_filing = replace(fund_filings[-1], fund_name=fund_name)
                active_filings.append(latest_filing)
                if latest_filing.accession in processed_accessions:
                    already_processed.append(latest_filing.accession)
                else:
                    new_filings.append(latest_filing)

        total_configured = len(self.configured_funds)
        filed_count = len(active_filings)
        effective_min = max(1, min(self.min_funds, total_configured))

        if review_reasons:
            status = PeriodStatus.NEEDS_REVIEW
            actionable = False
            reason = "; ".join(review_reasons)
        elif restated_processed:
            status = PeriodStatus.RESTATED
            actionable = True
            reason = "; ".join(changelog) or f"Period {period} has a new 13F-HR/A restatement."
        elif filed_count == 0:
            status = PeriodStatus.PENDING
            actionable = False
            reason = f"No filings detected for {period} (0/{total_configured} funds)."
        elif len(new_filings) == 0 and (
            len(missing_funds) == 0 or (self.allow_partial and filed_count >= effective_min)
        ):
            status = PeriodStatus.PROCESSED
            actionable = False
            reason = f"All {filed_count} active filings for {period} are already in state."
        elif len(missing_funds) == 0:
            status = PeriodStatus.COMPLETE
            actionable = len(new_filings) > 0
            reason = (
                f"All {total_configured}/{total_configured} configured funds have filed "
                f"for {period} ({len(new_filings)} new)."
            )
            if changelog:
                reason = f"{reason} Amendments: {'; '.join(changelog)}"
        else:
            status = PeriodStatus.PARTIAL
            actionable = bool(
                self.allow_partial and filed_count >= effective_min and len(new_filings) > 0
            )
            reason = (
                f"{filed_count}/{total_configured} funds have filed for {period} "
                f"(missing: {', '.join(missing_funds)}; allow_partial={self.allow_partial}, "
                f"min_funds={effective_min})."
            )

        return Period(
            period=period,
            status=status,
            filings=active_filings,
            new_filings=new_filings,
            already_processed=already_processed,
            missing_funds=missing_funds,
            superseded_accessions=superseded_accessions,
            changelog=changelog,
            review_reasons=review_reasons,
            reason=reason,
            actionable=actionable,
        )


def extract_numbers(text: str) -> set[str]:
    """Extract normalized numeric tokens from markdown text for fact-checking."""
    return {match.group(0).replace(",", "") for match in _NUMBER_TOKEN_RE.finditer(text)}
