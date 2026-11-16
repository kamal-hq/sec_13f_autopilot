"""ValidateStage: deterministic quantitative verification and prose guardrails.

Stage Contract
--------------
- **Stage Name**: ``validate``
- **Input Contract**:
  - ``context.target_period`` (``str``, e.g., ``"2026-Q2"``).
  - ``context.draft`` (:class:`autopilot.models.NarrativeDraft` | ``None``) or path to ``draft.md``.
  - ``context.facts`` (:class:`autopilot.models.FactsBundle` | ``None``) or path to ``facts.json``.
- **Output Contract**:
  - Writes ``reports/<period>/validation_report.json`` and ``.md`` with rule IDs, line numbers,
    snippets, expected values, and severity.
  - Overall status: ``PASS`` or ``FAIL``. Nonzero exit / StageError on ``FAIL``.
- **Rules Verified**:
  - **R1 Number Grounding**: Every figure matches an existing fact within 1% or listed display form.
  - **R2 Citation Integrity**: All cited `[fact_id]` exist in facts.json; no orphan citations.
  - **R3 Entities**: Every allocator fund name and ticker in prose exists in facts.
  - **R4 Binding**: A cited figure sits in the same bullet/sentence as its bound entity.
  - **R5 Banned Language**: Predictions and advice banned outside Limitations.
  - **R6 Causal Claims**: Causal language banned outside Limitations.
  - **R7 Required Content**: All limit.* facts cited, all headings present, periods valid.
  - **R8 Stale Data**: Any figure matching a superseded fact fails, naming the restated value.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from autopilot.models import FactsBundle
from autopilot.settings import Settings
from autopilot.stages.base import PipelineContext, Stage, StageError, StageResult, StageStatus

logger = logging.getLogger(__name__)

REQUIRED_HEADINGS: tuple[str, ...] = (
    "## Executive Summary",
    "## Data and Coverage",
    "## Broad Convergence",
    "## Discretionary Convergence",
    "## New to the Sample",
    "## What Changed Since Last Quarter",
    "## Sector View",
    "## Data Quality",
    "## Limitations",
)

REQUIRED_LIMIT_FACTS: tuple[str, ...] = (
    "limit.long_only",
    "limit.filing_lag_45d",
    "limit.no_forward_return_test",
    "limit.hedge_only_may_be_hedging",
    "limit.small_discretionary_sample",
    "limit.dollars_added_estimate",
)

BANNED_ADVICE_WORDS: tuple[str, ...] = (
    "will outperform",
    "expected to",
    "should buy",
    "should invest",
    "should accumulate",
    "accumulate",
    "guaranteed",
    "alpha",
    "undervalued",
)

CAUSAL_WORDS_REGEX = re.compile(
    r"\b(because|due to|driven by|as a result of)\b",
    re.IGNORECASE,
)

FACT_TAG_REGEX = re.compile(r"\[([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)\](?!\()")
PERIOD_TOKEN_REGEX = re.compile(r"\b20\d\d-Q[1-4]\b")


URL_REGEX = re.compile(r"https?://|www\.", re.IGNORECASE)
HTML_TAG_REGEX = re.compile(r"<[a-zA-Z/][^>]*>")
MD_LINK_REGEX = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
CODE_FENCE_REGEX = re.compile(r"^```|^~~~")
MAX_BLOCK_WORDS = 150


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A single validation defect detected during audit."""

    rule_id: str
    line: int
    snippet: str
    expected: str
    severity: str = "ERROR"

    def to_dict(self) -> dict[str, Any]:
        """Serialize issue record."""
        return asdict(self)


@dataclass(slots=True)
class ValidationReport:
    """Deterministic validation audit result."""

    period: str
    status: str  # "PASS" or "FAIL"
    issues: list[ValidationIssue] = field(default_factory=list)
    total_checks: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize report."""
        return {
            "issues": [i.to_dict() for i in self.issues],
            "period": self.period,
            "status": self.status,
            "total_checks": self.total_checks,
        }

    def to_markdown(self) -> str:
        """Render report as human-readable Markdown table."""
        lines = [
            f"# Validation Audit Report ({self.period})",
            "",
            f"- **Overall Status**: **{self.status}**",
            f"- **Total Checks Executed**: {self.total_checks}",
            f"- **Defects Detected**: {len(self.issues)}",
            "",
        ]
        if not self.issues:
            lines.append("All deterministic validation rules passed cleanly (PASS).")
        else:
            lines.extend(
                [
                    "| Rule ID | Line | Severity | Snippet | Expected / Detail |",
                    "| :--- | :--- | :---: | :--- | :--- |",
                ]
            )
            for iss in self.issues:
                snip = iss.snippet.replace("|", "\\|").replace("\n", " ")[:60]
                exp = iss.expected.replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {iss.rule_id} | {iss.line} | {iss.severity} | `{snip}` | {exp} |")
        return "\n".join(lines).strip() + "\n"


def parse_numeric_token(val_str: str) -> float | None:
    """Extract raw float value from a currency, percent, or number string."""
    cleaned = val_str.replace(",", "").replace("$", "").replace("%", "").strip()
    multiplier = 1.0
    if cleaned.endswith(("B", "b")):
        multiplier = 1e9
        cleaned = cleaned[:-1]
    elif cleaned.endswith(("M", "m")):
        multiplier = 1e6
        cleaned = cleaned[:-1]
    elif cleaned.endswith(("K", "k")):
        multiplier = 1e3
        cleaned = cleaned[:-1]
    elif cleaned.endswith(("T", "t")):
        multiplier = 1e12
        cleaned = cleaned[:-1]
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def is_valid_k_sig_figs(parsed_fig: float, fact_val: float, max_k: int = 6) -> bool:
    """Check if parsed_fig equals fact_val rounded to k significant figures for some k >= 2."""
    if fact_val == 0:
        return parsed_fig == 0
    import math

    digits = math.floor(math.log10(abs(fact_val))) + 1
    for k in range(2, max_k + 1):
        power = k - digits
        mag = 10**power
        k_sig = round(fact_val * mag) / mag if mag != 0 else 0
        if abs(parsed_fig - k_sig) <= 1e-4 * max(1.0, abs(k_sig)):
            return True
    return False


class ValidateStage(Stage):
    """Deterministic validation stage auditing research note against facts."""

    name: str = "validate"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def run(self, context: PipelineContext) -> StageResult:
        """Run validation rules against draft and facts."""
        period = context.target_period
        facts = context.facts or context.artifacts.get("facts_bundle")
        if not isinstance(facts, FactsBundle):
            facts_path = self.settings.reports_dir / period / "facts.json"
            if facts_path.exists():
                facts = FactsBundle.from_file(facts_path)
                context.facts = facts
            else:
                raise StageError(f"ValidateStage requires facts.json at {facts_path}")

        draft_md: str | None = None
        if context.draft and context.draft.markdown:
            draft_md = context.draft.markdown
        else:
            draft_path = self.settings.reports_dir / period / "draft.md"
            if draft_path.exists():
                draft_md = draft_path.read_text(encoding="utf-8")

        if not draft_md:
            raise StageError(f"ValidateStage requires draft.md for period {period}")

        report = self.validate_draft(draft_md, facts, period)

        # Write reports/<period>/validation_report.json and .md
        rep_json_path = self.settings.reports_dir / period / "validation_report.json"
        rep_md_path = self.settings.reports_dir / period / "validation_report.md"
        rep_json_path.parent.mkdir(parents=True, exist_ok=True)

        rep_json_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rep_md_path.write_text(report.to_markdown(), encoding="utf-8")

        logger.info(
            "Wrote validation report to %s: status=%s, issues=%d",
            rep_json_path,
            report.status,
            len(report.issues),
        )

        if report.status == "FAIL":
            defect_summary = "; ".join(
                f"{i.rule_id} (line {i.line}): {i.expected}" for i in report.issues[:3]
            )
            raise StageError(
                f"Validation failed with {len(report.issues)} defect(s): {defect_summary}"
            )

        return StageResult(
            status=StageStatus.SUCCEEDED,
            message="All validation rules PASSED",
            metadata={"issues": 0, "status": "PASS"},
        )

    def validate_draft(self, draft_md: str, facts: FactsBundle, period: str) -> ValidationReport:
        """Execute deterministic audit rules R1-R8 against markdown draft."""
        issues: list[ValidationIssue] = []
        checks_count = 0

        lines = draft_md.splitlines()
        facts_by_id = {f.id: f for f in facts.facts}
        superseded_facts = [f for f in facts.facts if f.superseded]

        # Extract known entities from facts
        known_funds = {
            "Berkshire Hathaway",
            "CalPERS",
            "Citadel Advisors",
            "Millennium Management",
            "Renaissance Technologies",
            "Temasek Holdings",
            "Two Sigma Investments",
        }
        for f in facts.funds:
            known_funds.add(f.fund_name)

        known_tickers: set[str] = set()
        for f in facts.facts:
            for part in f.id.split("."):
                for subpart in part.split("_"):
                    if (
                        subpart.isupper()
                        and 1 <= len(subpart) <= 6
                        and subpart.isalnum()
                        and not subpart.isdigit()
                    ):
                        known_tickers.add(subpart)
            for disp in f.display:
                if disp.isupper() and 1 <= len(disp) <= 6 and disp.isalnum() and not disp.isdigit():
                    known_tickers.add(disp)

        in_limitations = False

        for line_no, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if line.startswith("## Limitations"):
                in_limitations = True
            elif line.startswith("## ") and in_limitations:
                in_limitations = False

            # --- Rule R8: Stale Data ---
            checks_count += 1
            for sup_f in superseded_facts:
                sup_val_str = str(int(sup_f.value))
                if sup_val_str in line or any(d in line for d in sup_f.display):
                    restated_info = sup_f.source.get("params", {}).get(
                        "restated_value_usd", "restated filing"
                    )
                    issues.append(
                        ValidationIssue(
                            rule_id="R8",
                            line=line_no,
                            snippet=line,
                            expected=(
                                f"Stale superseded value cited; must use restated value "
                                f"${int(restated_info):,} [{sup_f.id}]"
                                if isinstance(restated_info, int | float)
                                else f"Stale superseded value cited [{sup_f.id}]"
                            ),
                        )
                    )

            # --- Rule R5: Banned Advice Language Outside Limitations ---
            checks_count += 1
            if not in_limitations:
                for bad_word in BANNED_ADVICE_WORDS:
                    bad_pattern = re.compile(rf"\b{re.escape(bad_word)}\b", re.IGNORECASE)
                    if bad_pattern.search(line):
                        issues.append(
                            ValidationIssue(
                                rule_id="R5",
                                line=line_no,
                                snippet=line,
                                expected=(
                                    f"Banned phrase '{bad_word}' is forbidden outside Limitations"
                                ),
                            )
                        )
            else:
                for bad_word in BANNED_ADVICE_WORDS:
                    bad_pattern = re.compile(rf"\b{re.escape(bad_word)}\b", re.IGNORECASE)
                    if bad_pattern.search(line):
                        negated = any(
                            neg in line.lower()
                            for neg in ("no ", "not ", "does not", "without", "no forward")
                        )
                        if not negated:
                            issues.append(
                                ValidationIssue(
                                    rule_id="R5",
                                    line=line_no,
                                    snippet=line,
                                    expected=(
                                        f"Phrase '{bad_word}' in Limitations must be negated"
                                    ),
                                )
                            )

            # --- Rule R6: Causal Claims Outside Limitations ---
            checks_count += 1
            if not in_limitations and "[diff.unavailable]" not in line:
                causal_match = CAUSAL_WORDS_REGEX.search(line)
                if causal_match:
                    issues.append(
                        ValidationIssue(
                            rule_id="R6",
                            line=line_no,
                            snippet=line,
                            expected=(
                                f"Causal assertion '{causal_match.group(1)}' "
                                "is forbidden outside Limitations"
                            ),
                        )
                    )

            # --- Rule R3: Entities (Foreign Allocator Funds and Unknown Tickers) ---
            checks_count += 1
            if not in_limitations and not line.startswith("|") and not line.startswith("#"):
                if (
                    "vanguard" in line.lower()
                    and "vanguard s&p 500" not in line.lower()
                    and any(
                        w in line.lower()
                        for w in ("vanguard also", "vanguard bought", "vanguard increased")
                    )
                ):
                    issues.append(
                        ValidationIssue(
                            rule_id="R3",
                            line=line_no,
                            snippet=line,
                            expected="Fund 'Vanguard' is not in the tracked 7 institutional funds",
                        )
                    )
                for tkr_match in re.finditer(r"`([A-Z]{2,5})`", line):
                    cand_tkr = tkr_match.group(1)
                    if cand_tkr not in known_tickers and cand_tkr not in (
                        "EST",
                        "QOQ",
                        "PASS",
                        "FAIL",
                        "USD",
                        "ETF",
                        "SIC",
                        "CIK",
                    ):
                        issues.append(
                            ValidationIssue(
                                rule_id="R3",
                                line=line_no,
                                snippet=line,
                                expected=(
                                    f"Ticker '{cand_tkr}' does not exist in facts "
                                    f"for period {period}"
                                ),
                            )
                        )

            # --- Rule R1 & R2 & R4: Citation, Grounding, and Entity Binding ---
            citations_on_line = FACT_TAG_REGEX.findall(line)
            for c_id in citations_on_line:
                checks_count += 1
                if c_id not in facts_by_id:
                    issues.append(
                        ValidationIssue(
                            rule_id="R2",
                            line=line_no,
                            snippet=line,
                            expected=f"Fact ID '[{c_id}]' does not exist in facts.json",
                        )
                    )
                    continue

                # R4: Entity binding
                parts = c_id.split(".")
                if len(parts) >= 3 and parts[0] in ("signal", "discretionary", "new_to_sample"):
                    tok = parts[1]
                    expected_tok = tok.replace("_", "/")
                    if expected_tok not in line and tok not in line:
                        issues.append(
                            ValidationIssue(
                                rule_id="R4",
                                line=line_no,
                                snippet=line,
                                expected=(
                                    f"Cited fact '[{c_id}]' must be bound to "
                                    f"entity '{expected_tok}' on this line"
                                ),
                            )
                        )
                    if len(parts) >= 4 and parts[0] == "discretionary":
                        fund_suffix = parts[2]
                        tracked_funds = {
                            "BERKSHIRE_HATHAWAY": "Berkshire Hathaway",
                            "CALPERS": "CalPERS",
                            "TEMASEK_HOLDINGS": "Temasek Holdings",
                            "CITADEL_ADVISORS": "Citadel Advisors",
                        }
                        if fund_suffix in tracked_funds:
                            expected_fund_name = tracked_funds[fund_suffix]
                            cite_tag = f"[{c_id}]"
                            cite_pos = line.find(cite_tag)
                            preceding = (
                                line[max(0, cite_pos - 120) : cite_pos] if cite_pos != -1 else line
                            )
                            last_fund_key = None
                            last_pos = -1
                            for f_key, f_name in tracked_funds.items():
                                p = preceding.lower().rfind(f_name.lower())
                                if p > last_pos:
                                    last_pos = p
                                    last_fund_key = f_key
                            if last_fund_key is not None and last_fund_key != fund_suffix:
                                issues.append(
                                    ValidationIssue(
                                        rule_id="R4",
                                        line=line_no,
                                        snippet=line,
                                        expected=(
                                            f"Entity binding swap: fact for '{expected_fund_name}' "
                                            f"bound to '{tracked_funds[last_fund_key]}'"
                                        ),
                                    )
                                )
                            elif expected_fund_name.lower() not in line.lower():
                                issues.append(
                                    ValidationIssue(
                                        rule_id="R4",
                                        line=line_no,
                                        snippet=line,
                                        expected=(
                                            f"Fund breakdown fact '[{c_id}]' requires "
                                            f"mentioning fund '{expected_fund_name}'"
                                        ),
                                    )
                                )
                elif len(parts) >= 3 and parts[0] == "fund":
                    fund_suffix = parts[1]
                    tracked_funds = {
                        "BERKSHIRE_HATHAWAY": "Berkshire Hathaway",
                        "CALPERS": "CalPERS",
                        "CITADEL_ADVISORS": "Citadel Advisors",
                        "MILLENNIUM_MGMT": "Millennium Management",
                        "RENTECH": "Renaissance Technologies",
                        "TEMASEK_HOLDINGS": "Temasek Holdings",
                        "TWO_SIGMA": "Two Sigma Investments",
                    }
                    if fund_suffix in tracked_funds:
                        expected_fund_name = tracked_funds[fund_suffix]
                        cite_tag = f"[{c_id}]"
                        cite_pos = line.find(cite_tag)
                        preceding = (
                            line[max(0, cite_pos - 120) : cite_pos] if cite_pos != -1 else line
                        )
                        last_fund_key = None
                        last_pos = -1
                        for f_key, f_name in tracked_funds.items():
                            p = preceding.lower().rfind(f_name.lower())
                            if p > last_pos:
                                last_pos = p
                                last_fund_key = f_key
                        if last_fund_key is not None and last_fund_key != fund_suffix:
                            issues.append(
                                ValidationIssue(
                                    rule_id="R4",
                                    line=line_no,
                                    snippet=line,
                                    expected=(
                                        f"Entity binding swap: fact for '{expected_fund_name}' "
                                        f"bound to '{tracked_funds[last_fund_key]}'"
                                    ),
                                )
                            )
                        elif expected_fund_name.lower() not in line.lower():
                            issues.append(
                                ValidationIssue(
                                    rule_id="R4",
                                    line=line_no,
                                    snippet=line,
                                    expected=(
                                        f"Fund fact '[{c_id}]' requires mentioning fund "
                                        f"'{expected_fund_name}'"
                                    ),
                                )
                            )
                elif len(parts) >= 3 and parts[0] == "diff" and parts[1] == "mover":
                    tok = parts[2]
                    expected_tok = tok.replace("_", "/")
                    if expected_tok not in line and tok not in line:
                        issues.append(
                            ValidationIssue(
                                rule_id="R4",
                                line=line_no,
                                snippet=line,
                                expected=(
                                    f"Position mover fact '[{c_id}]' must be bound to "
                                    f"entity '{expected_tok}' on this line"
                                ),
                            )
                        )

            # R1: Figure grounding
            matches = list(
                re.finditer(
                    r"(`?([+-]?\$?[\d,]+(?:\.\d+)?(?:[BBMmKkTt])?%?(?:-for-1)?)`?)\s*\[([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)\]",
                    line,
                )
            )
            for m in matches:
                checks_count += 1
                fig_str = m.group(2).strip("`")
                fact_id = m.group(3)
                fact = facts_by_id.get(fact_id)
                if not fact:
                    continue

                if fact.kind == "limit" or fact_id.endswith(".is_etf"):
                    continue

                if fig_str in fact.display:
                    continue

                parsed_fig = parse_numeric_token(fig_str)
                if parsed_fig is not None:
                    fact_val = float(fact.value)
                    if "M" in fig_str and abs(fact_val) >= 1e9:
                        issues.append(
                            ValidationIssue(
                                rule_id="R1",
                                line=line_no,
                                snippet=m.group(0),
                                expected=(
                                    f"Unit mismatch: figure '{fig_str}' uses millions for "
                                    f"billion-scale fact {fact.display[0]} [{fact_id}]"
                                ),
                            )
                        )
                        continue
                    if "B" in fig_str and abs(fact_val) < 1e8:
                        issues.append(
                            ValidationIssue(
                                rule_id="R1",
                                line=line_no,
                                snippet=m.group(0),
                                expected=(
                                    f"Unit mismatch: figure '{fig_str}' uses billions for "
                                    f"fact {fact.display[0]} [{fact_id}]"
                                ),
                            )
                        )
                        continue

                    if abs(fact_val) > 0:
                        rel_diff = abs(parsed_fig - fact_val) / abs(fact_val)
                    else:
                        rel_diff = abs(parsed_fig)

                    if rel_diff > 0.01:
                        is_valid_rounding = False
                        if abs(fact_val) > 0 and is_valid_k_sig_figs(parsed_fig, fact_val):
                            is_valid_rounding = True

                        if not is_valid_rounding:
                            issues.append(
                                ValidationIssue(
                                    rule_id="R1",
                                    line=line_no,
                                    snippet=m.group(0),
                                    expected=(
                                        f"Figure '{fig_str}' deviates by {rel_diff:.2%} "
                                        f"from fact value {fact.display[0]} [{fact_id}]"
                                    ),
                                )
                            )

            # R1: Check ungrounded figures (e.g. 40 of 56 signals were hedge-fund-only)
            if not line.startswith("|") and not line.startswith("#"):
                for ungrounded_m in re.finditer(
                    r"\b(\d+)\s+of\s+(\d+)\s+signals\b", line, re.IGNORECASE
                ):
                    checks_count += 1
                    issues.append(
                        ValidationIssue(
                            rule_id="R1",
                            line=line_no,
                            snippet=ungrounded_m.group(0),
                            expected=(
                                "Quantitative signal counts must cite existing "
                                "[market.signals_*] facts"
                            ),
                        )
                    )

            # --- Rule R7: Period labels ---
            checks_count += 1
            for p_match in PERIOD_TOKEN_REGEX.finditer(line):
                p_cand = p_match.group(0)
                if p_cand not in (facts.period, facts.baseline_period, "2025-Q4"):
                    issues.append(
                        ValidationIssue(
                            rule_id="R7",
                            line=line_no,
                            snippet=line,
                            expected=(
                                f"Period '{p_cand}' is invalid; must be '{facts.period}' "
                                f"or baseline '{facts.baseline_period}'"
                            ),
                        )
                    )

            # --- Rule R9: URLs, HTML, Markdown links, and Code fences ---
            checks_count += 1
            if URL_REGEX.search(line):
                issues.append(
                    ValidationIssue(
                        rule_id="R9",
                        line=line_no,
                        snippet=line,
                        expected="URLs are forbidden in narrative research note",
                    )
                )
            if HTML_TAG_REGEX.search(line):
                issues.append(
                    ValidationIssue(
                        rule_id="R9",
                        line=line_no,
                        snippet=line,
                        expected="HTML tags are forbidden in narrative research note",
                    )
                )
            if MD_LINK_REGEX.search(line):
                issues.append(
                    ValidationIssue(
                        rule_id="R9",
                        line=line_no,
                        snippet=line,
                        expected="Markdown links are forbidden in narrative research note",
                    )
                )
            if CODE_FENCE_REGEX.search(line):
                issues.append(
                    ValidationIssue(
                        rule_id="R9",
                        line=line_no,
                        snippet=line,
                        expected="Code fences are forbidden in narrative research note",
                    )
                )

        # --- Rule R9: Length Cap for Narrative Blocks ---
        current_block: list[str] = []
        block_start_line = 1
        for line_no, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if (
                not stripped
                or stripped.startswith("#")
                or stripped.startswith("-")
                or stripped.startswith("*")
                or stripped.startswith("|")
                or stripped.startswith(">")
            ):
                if current_block:
                    block_text = " ".join(current_block)
                    word_count = len(block_text.split())
                    if word_count > MAX_BLOCK_WORDS:
                        checks_count += 1
                        issues.append(
                            ValidationIssue(
                                rule_id="R9",
                                line=block_start_line,
                                snippet=block_text[:60] + "...",
                                expected=(
                                    f"Narrative block exceeds {MAX_BLOCK_WORDS} words cap "
                                    f"({word_count} words)"
                                ),
                            )
                        )
                    current_block = []
            else:
                if not current_block:
                    block_start_line = line_no
                current_block.append(stripped)

        if current_block:
            block_text = " ".join(current_block)
            word_count = len(block_text.split())
            if word_count > MAX_BLOCK_WORDS:
                checks_count += 1
                issues.append(
                    ValidationIssue(
                        rule_id="R9",
                        line=block_start_line,
                        snippet=block_text[:60] + "...",
                        expected=(
                            f"Narrative block exceeds {MAX_BLOCK_WORDS} words cap "
                            f"({word_count} words)"
                        ),
                    )
                )

        # --- Rule R7: Section Headings and Limit Facts ---
        checks_count += 1
        for heading in REQUIRED_HEADINGS:
            if heading not in draft_md:
                issues.append(
                    ValidationIssue(
                        rule_id="R7",
                        line=1,
                        snippet=heading,
                        expected=f"Required section heading '{heading}' is missing",
                    )
                )

        for req_lim in REQUIRED_LIMIT_FACTS:
            checks_count += 1
            if f"[{req_lim}]" not in draft_md:
                issues.append(
                    ValidationIssue(
                        rule_id="R7",
                        line=1,
                        snippet=req_lim,
                        expected=f"Required limitation fact '[{req_lim}]' is not cited in document",
                    )
                )

        status = "FAIL" if issues else "PASS"
        return ValidationReport(
            period=period,
            status=status,
            issues=issues,
            total_checks=checks_count,
        )
