"""PublishStage: writes finalized reports, CSV exports, PR body, and optional state checkpoint.

Stage Contract
--------------
- **Stage Name**: ``publish``
- **Input Contract**:
  - ``context.target_period`` (``str``, e.g., ``"2026-Q2"``).
  - ``context.draft`` (:class:`autopilot.models.NarrativeDraft` | ``None``) or ``draft.md``.
  - ``context.facts`` (:class:`autopilot.models.FactsBundle` | ``None``) or ``facts.json``.
  - ``context.commit_state`` (``bool``): If ``True`` (and ``not context.replay``), marks the
    period's accessions as processed in ``state/processed_filings.json``.
  - ``context.replay`` (``bool``): If ``True``, labels outputs ``replay`` and never modifies state.
- **Output Contract**:
  - Writes ``context.publish`` as a :class:`autopilot.models.PublishResult` containing:
    - ``period`` (``str``): Published reporting quarter.
    - ``published_files`` (``list[str]``): Paths of finalized report and metrics artifacts
      (``final.md``, ``final_cited.md``, ``validation_report.md``, ``diff.md``, ``run.json``,
      ``pr_body.md``).
    - ``export_files`` (``list[str]``): Paths of ``exports/signals.csv``,
      ``exports/discretionary.csv``, ``exports/new_to_sample.csv``, ``exports/fund_summary.csv``.
    - ``state_committed`` (``bool``): Whether ``state/processed_filings.json`` was updated.
    - ``replay`` (``bool``): Whether outputs were labeled as replay.
- **Side Effects & Invariants**:
  - Refuses to publish if ``ValidateStage`` does not pass (``status == "PASS"``).
  - Leaves ``state/processed_filings.json`` completely untouched unless ``--commit-state`` is set
    and ``--replay`` is ``False``.
  - Never writes secrets to ``run.json`` or any published artifact.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any

from autopilot.models import (
    FactItem,
    FactsBundle,
    PipelineContext,
    PublishResult,
    StageStatus,
)
from autopilot.settings import Settings, redact_text
from autopilot.stages.base import Stage, StageError, StageResult
from autopilot.stages.validate import ValidateStage, ValidationReport
from autopilot.state import StateStore

logger = logging.getLogger(__name__)

_CITED_FIGURE_RE = re.compile(
    r"`?([+-]?\$?[\d,]+(?:\.\d+)?(?:[BBMmKkTt])?%?(?:-for-1)?)`?"
    r"\s*\[([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)\]`?(?!\()"
)
_ORPHAN_TAG_RE = re.compile(r"\s*\[([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)\](?!\()")
_BACKTICK_NUMBER_RE = re.compile(r"`([+-]?\$?[\d,]+(?:\.\d+)?(?:[BBMmKkTt])?%?(?:-for-1)?)`")
_SHORT_USD_1DEC_RE = re.compile(r"^-?\$\d+(?:\.\d)?[TBMK]$")
_SHORT_USD_ANY_RE = re.compile(r"^-?\$\d+(?:\.\d+)?[TBMK]$")


def _select_short_usd_display(fact: FactItem, raw_fig: str) -> str:
    """Select short display form (such as '$917.2B') from fact.display when available."""
    chosen: str | None = None
    for disp in fact.display:
        s = str(disp).strip()
        if _SHORT_USD_1DEC_RE.match(s):
            chosen = s
            break
    if chosen is None:
        for disp in fact.display:
            s = str(disp).strip()
            if _SHORT_USD_ANY_RE.match(s):
                chosen = s
                break
    if chosen is None:
        chosen = raw_fig
    if raw_fig.startswith("+") and not chosen.startswith(("+", "-")):
        return f"+{chosen}"
    return chosen


def _apply_replay_banner(markdown: str, *, replay: bool) -> str:
    """Attach a deterministic [REPLAY] label to the top heading and banner when replay=True."""
    if not replay:
        return markdown
    lines = markdown.splitlines()
    if lines and lines[0].startswith("# "):
        if "[REPLAY]" not in lines[0]:
            lines[0] = f"{lines[0]} [REPLAY]"
        banner = "> **Mode**: `replay` (historical / non-state-mutating execution)"
        if len(lines) < 2 or banner not in "\n".join(lines[:4]):
            lines.insert(1, "")
            lines.insert(2, banner)
        return "\n".join(lines).strip() + "\n"
    return f"# [REPLAY]\n\n{markdown.strip()}\n"


def render_final_clean(
    draft_md: str,
    facts: FactsBundle | None = None,
    *,
    replay: bool = False,
) -> str:
    """Render clean `final.md` without figure backticks or citations, using short USD."""

    def _replace_cited_fig(match: re.Match[str]) -> str:
        fig_str = match.group(1).strip("`")
        fact_id = match.group(2)
        if facts is not None and "$" in fig_str:
            fact = facts.get_fact(fact_id)
            if fact is not None and not fact.superseded:
                return _select_short_usd_display(fact, fig_str)
        return fig_str

    cleaned = _CITED_FIGURE_RE.sub(_replace_cited_fig, draft_md)
    cleaned = _ORPHAN_TAG_RE.sub("", cleaned)
    cleaned = _BACKTICK_NUMBER_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r" +([.,;:)\]])", r"\1", cleaned)
    cleaned_lines = [line.rstrip() for line in cleaned.splitlines()]
    normalized = "\n".join(cleaned_lines).strip() + "\n"
    return _apply_replay_banner(normalized, replay=replay)


def render_final_cited(draft_md: str, *, replay: bool = False) -> str:
    """Render cited `final_cited.md` preserving all `[fact_id]` provenance tags."""
    lines = [line.rstrip() for line in draft_md.splitlines()]
    normalized = "\n".join(lines).strip() + "\n"
    return _apply_replay_banner(normalized, replay=replay)


def write_csv_exports(facts: FactsBundle, exports_dir: Path) -> list[Path]:
    """Write deterministic CSV exports (signals, discretionary, new_to_sample, fund_summary)."""
    exports_dir.mkdir(parents=True, exist_ok=True)
    period = facts.period
    raw = facts.raw_metrics or {}
    written: list[Path] = []

    # 1. signals.csv
    signals_path = exports_dir / "signals.csv"
    broad_rows = list(raw.get("broad_signals") or [])
    with signals_path.open("w", encoding="utf-8", newline="") as f_sig:
        writer = csv.writer(f_sig, lineterminator="\n")
        writer.writerow(
            [
                "period",
                "ticker",
                "company_name",
                "cusip",
                "sector",
                "funds_buying",
                "dollars_added",
                "hedge_fund_buyers",
                "discretionary_buyers",
                "buyer_mix",
                "fund_type_breakdown",
                "funds",
            ]
        )
        for row in broad_rows:
            writer.writerow(
                [
                    period,
                    row.get("ticker", ""),
                    row.get("company_name", ""),
                    row.get("cusip", ""),
                    row.get("sector", ""),
                    row.get("funds_buying", 0),
                    row.get("dollars_added", 0),
                    row.get("hedge_fund_buyers", 0),
                    row.get("discretionary_buyers", 0),
                    row.get("buyer_mix", ""),
                    row.get("fund_type_breakdown", ""),
                    "; ".join(row.get("funds") or []),
                ]
            )
    written.append(signals_path)

    # 2. discretionary.csv
    disc_path = exports_dir / "discretionary.csv"
    disc_rows = list(raw.get("discretionary_signals") or [])
    with disc_path.open("w", encoding="utf-8", newline="") as f_disc:
        writer = csv.writer(f_disc, lineterminator="\n")
        writer.writerow(
            [
                "period",
                "ticker",
                "company_name",
                "cusip",
                "sector",
                "funds_buying",
                "dollars_added",
                "fund_type_breakdown",
                "funds",
                "fund_dollars_added",
            ]
        )
        for row in disc_rows:
            fund_adds = row.get("fund_dollars_added") or {}
            fund_adds_str = "; ".join(f"{k}:{v}" for k, v in sorted(fund_adds.items()))
            writer.writerow(
                [
                    period,
                    row.get("ticker", ""),
                    row.get("company_name", ""),
                    row.get("cusip", ""),
                    row.get("sector", ""),
                    row.get("funds_buying", 0),
                    row.get("dollars_added", 0),
                    row.get("fund_type_breakdown", ""),
                    "; ".join(row.get("funds") or []),
                    fund_adds_str,
                ]
            )
    written.append(disc_path)

    # 3. new_to_sample.csv
    new_path = exports_dir / "new_to_sample.csv"
    new_rows = list(raw.get("new_to_sample") or [])
    with new_path.open("w", encoding="utf-8", newline="") as f_new:
        writer = csv.writer(f_new, lineterminator="\n")
        writer.writerow(
            [
                "period",
                "ticker",
                "company_name",
                "cusip",
                "sector",
                "funds_buying",
                "dollars_added",
                "fund_type_breakdown",
                "funds",
            ]
        )
        for row in new_rows:
            writer.writerow(
                [
                    period,
                    row.get("ticker", ""),
                    row.get("company_name", ""),
                    row.get("cusip", ""),
                    row.get("sector", ""),
                    row.get("funds_buying", 0),
                    row.get("dollars_added", 0),
                    row.get("fund_type_breakdown", ""),
                    "; ".join(row.get("funds") or []),
                ]
            )
    written.append(new_path)

    # 4. fund_summary.csv
    fund_path = exports_dir / "fund_summary.csv"
    fund_rows = list(raw.get("fund_aum") or [])
    with fund_path.open("w", encoding="utf-8", newline="") as f_fund:
        writer = csv.writer(f_fund, lineterminator="\n")
        writer.writerow(
            [
                "period",
                "cik",
                "fund_name",
                "fund_type",
                "accession",
                "filed_date",
                "active_positions",
                "aum_equity_only_usd",
                "aum_equity_only_prev_usd",
                "aum_equity_change_usd",
                "aum_with_options_usd",
                "aum_with_options_prev_usd",
                "top_10_concentration_pct",
                "reconciled",
            ]
        )
        for row in fund_rows:
            writer.writerow(
                [
                    period,
                    row.get("cik", ""),
                    row.get("fund_name", ""),
                    row.get("fund_type", ""),
                    row.get("accession", ""),
                    row.get("filed_date", ""),
                    row.get("active_positions", 0),
                    row.get("aum_equity_only_usd", 0),
                    row.get("aum_equity_only_prev_usd", 0),
                    row.get("aum_equity_change_usd", 0),
                    row.get("aum_with_options_usd", 0),
                    row.get("aum_with_options_prev_usd", 0),
                    row.get("top_10_concentration_pct", 0.0),
                    row.get("reconciled", True),
                ]
            )
    written.append(fund_path)

    return written


def _extract_diff_summary(diff_markdown: str, max_lines: int = 22) -> str:
    """Extract a concise summary block from diff.md for the Pull Request body."""
    if not diff_markdown or not diff_markdown.strip():
        return "- Quarter-over-quarter diff summary unavailable."

    kept: list[str] = []
    for line in diff_markdown.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("# "):
            continue
        if stripped.startswith("*Generated deterministically"):
            break
        if stripped.startswith("## "):
            kept.append(f"#{line}")
        else:
            kept.append(line)
        if len(kept) >= max_lines:
            break
    return "\n".join(kept).strip() if kept else "- Quarter-over-quarter diff summary unavailable."


def build_pr_body(
    period: str,
    diff_markdown: str,
    validation_report: ValidationReport | dict[str, Any],
    run_metadata: dict[str, Any],
    *,
    replay: bool = False,
) -> str:
    """Build deterministic Pull Request body markdown for the human review gate."""
    val_dict = (
        validation_report.to_dict()
        if isinstance(validation_report, ValidationReport)
        else dict(validation_report or {})
    )
    val_status = str(val_dict.get("status", "PASS")).upper()
    total_checks = int(val_dict.get("total_checks", 0))
    issues = list(val_dict.get("issues") or [])

    rule_counts: dict[str, int] = {f"R{i}": 0 for i in range(1, 10)}
    for iss in issues:
        rid = str(iss.get("rule_id", ""))
        if rid:
            rule_counts[rid] = rule_counts.get(rid, 0) + 1

    llm_meta = dict(run_metadata.get("llm") or {})
    llm_mode = str(llm_meta.get("llm_mode") or run_metadata.get("llm_mode") or "none")
    if llm_mode == "null":
        llm_mode = "none"
    model_name = str(llm_meta.get("model") or "deterministic-template")
    attempts = int(llm_meta.get("attempts") or 0)
    final_status = str(
        llm_meta.get("final_status") or run_metadata.get("llm_status") or "deterministic"
    )
    retry_failures = dict(llm_meta.get("validation_failures_per_rule") or {})

    stage_seconds: dict[str, float] = {}
    raw_stage_sec = run_metadata.get("stage_seconds") or run_metadata.get("stage_timings")
    if isinstance(raw_stage_sec, dict):
        stage_seconds = {str(k): float(v) for k, v in raw_stage_sec.items()}
    elif isinstance(run_metadata.get("stages"), list):
        for st_entry in run_metadata["stages"]:
            if isinstance(st_entry, dict) and st_entry.get("stage_name"):
                dur_ms = float(st_entry.get("duration_ms", 0.0))
                stage_seconds[str(st_entry["stage_name"])] = round(dur_ms / 1000.0, 3)
    total_stage_sec = round(sum(float(v) for v in stage_seconds.values()), 3)

    replay_badge = " `[REPLAY]`" if replay else ""
    diff_excerpt = _extract_diff_summary(diff_markdown)

    rule_rows: list[str] = []
    rule_labels = {
        "R1": "Number Grounding",
        "R2": "Citation Integrity",
        "R3": "Tracked Entities",
        "R4": "Entity Binding",
        "R5": "Banned Predictions/Advice",
        "R6": "Causal Claims",
        "R7": "Required Content & Limits",
        "R8": "Stale Superseded Data",
        "R9": "Formatting & Length Cap",
    }
    for r_id in [f"R{i}" for i in range(1, 10)]:
        final_cnt = rule_counts.get(r_id, 0)
        retry_cnt = int(retry_failures.get(r_id, 0))
        r_status = "PASS" if final_cnt == 0 else "FAIL"
        rule_rows.append(
            f"| **{r_id}** ({rule_labels[r_id]}) | {r_status} | {final_cnt} | {retry_cnt} |"
        )

    stage_rows: list[str] = []
    if stage_seconds:
        for st_name, sec_val in stage_seconds.items():
            stage_rows.append(f"| `{st_name}` | `{float(sec_val):.3f}s` |")
        stage_rows.append(f"| **Total** | **`{total_stage_sec:.3f}s`** |")
    else:
        stage_rows.append("| `(standalone publish)` | `0.000s` |")

    lines = [
        f"# Quarterly 13F Research Note Review Gate — {period}{replay_badge}",
        "",
        "## 1. Quarter-over-Quarter Diff Summary (`diff.md`)",
        "",
        diff_excerpt,
        "",
        f"## 2. Deterministic Validation Audit: **{val_status}**",
        "",
        f"- **Overall Status**: **{val_status}** ({total_checks} checks executed, "
        f"{len(issues)} final defects)",
        "",
        "| Rule | Final Status | Final Defects | Retry Defects Caught |",
        "| :--- | :---: | :---: | :---: |",
        *rule_rows,
        "",
        "## 3. Execution & LLM Metrics",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Period** | `{period}` |",
        f"| **Execution Mode** | `{'replay' if replay else 'standard'}` |",
        f"| **LLM Mode** | `{llm_mode}` |",
        f"| **Model** | `{model_name}` |",
        f"| **Narrative Attempts** | `{attempts}` |",
        f"| **Final Narrative Status** | `{final_status}` |",
        "",
        "### Stage Timings",
        "",
        "| Stage | Duration (s) |",
        "| :--- | ---: |",
        *stage_rows,
        "",
        "## 4. Mandatory Human Reviewer Checklist",
        "",
        "- [ ] **Spot-check 3 cited figures against the filings XML or cover pages** "
        f"(verify 3 cited numbers in `reports/{period}/final_cited.md` against raw SEC Form 13F)",
        "- [ ] **Check issuer names against nameOfIssuer** "
        "(verify deterministic `nameOfIssuer` display names and share-class merges "
        "such as `GOOGL/GOOG`)",
        "- [ ] **Confirm all required limitations are present** "
        f"(verify all 6 required `limit.*` disclosures appear in `reports/{period}/final.md`)",
        "- [ ] **Confirm no predictive language or ungrounded causal claims** "
        "(verify absence of forward-looking return predictions, trade advice, or causal claims)",
        "",
        "## 5. Published Artifacts",
        "",
        f"- Clean Research Note: [`reports/{period}/final.md`](reports/{period}/final.md)",
        (
            f"- Cited Research Note: "
            f"[`reports/{period}/final_cited.md`](reports/{period}/final_cited.md)"
        ),
        (
            f"- Validation Report: "
            f"[`reports/{period}/validation_report.md`](reports/{period}/validation_report.md)"
        ),
        f"- QoQ Diff Summary: [`reports/{period}/diff.md`](reports/{period}/diff.md)",
        f"- Facts Bundle: [`reports/{period}/facts.json`](reports/{period}/facts.json)",
        f"- Run Metadata: [`reports/{period}/run.json`](reports/{period}/run.json)",
        (
            f"- Broad Signals CSV: "
            f"[`reports/{period}/exports/signals.csv`](reports/{period}/exports/signals.csv)"
        ),
        (
            f"- Discretionary Signals CSV: "
            f"[`reports/{period}/exports/discretionary.csv`]"
            f"(reports/{period}/exports/discretionary.csv)"
        ),
        (
            f"- New-to-Sample CSV: "
            f"[`reports/{period}/exports/new_to_sample.csv`]"
            f"(reports/{period}/exports/new_to_sample.csv)"
        ),
        (
            f"- Fund Summary CSV: "
            f"[`reports/{period}/exports/fund_summary.csv`]"
            f"(reports/{period}/exports/fund_summary.csv)"
        ),
        "",
    ]
    return "\n".join(lines)


class PublishStage(Stage):
    """Publishes validated research notes, CSV exports, PR body, and optional state checkpoint."""

    name: str = "publish"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        commit_state: bool = False,
        replay: bool = False,
    ) -> None:
        self.settings = settings or Settings()
        self.commit_state = commit_state
        self.replay = replay

    def _resolve_period_filings(
        self,
        period: str,
        context: PipelineContext,
        facts: FactsBundle,
    ) -> list[dict[str, Any]]:
        """Collect accession metadata for `period` to record in `state/processed_filings.json`."""
        by_acc: dict[str, dict[str, Any]] = {}

        # 1. From facts.raw_metrics (fund_aum + coverage.amendments_applied + reconciliations)
        raw = facts.raw_metrics or {}
        amendments_by_acc: dict[str, list[str]] = {}
        for amd in (raw.get("coverage") or {}).get("amendments_applied") or []:
            acc = str(amd.get("accession", ""))
            if acc:
                amendments_by_acc[acc] = list(amd.get("supersedes") or [])

        for f_row in raw.get("fund_aum") or []:
            acc = str(f_row.get("accession", ""))
            if acc:
                by_acc[acc] = {
                    "accession": acc,
                    "filed_date": str(f_row.get("filed_date", "")),
                    "period": period,
                    "supersedes": amendments_by_acc.get(acc),
                }

        for rec in (raw.get("coverage") or {}).get("reconciliations") or []:
            if str(rec.get("period", "")) == period:
                acc = str(rec.get("accession", ""))
                if acc and acc not in by_acc:
                    by_acc[acc] = {
                        "accession": acc,
                        "filed_date": "",
                        "period": period,
                        "supersedes": amendments_by_acc.get(acc),
                    }

        # 2. Enrich from detect.json if present
        detect_path = self.settings.reports_dir / period / "detect.json"
        if detect_path.exists():
            try:
                det_data = json.loads(detect_path.read_text(encoding="utf-8"))
                for f_item in det_data.get("new_filings") or []:
                    if str(f_item.get("period", "")) == period:
                        acc = str(f_item.get("accession", ""))
                        if acc:
                            by_acc[acc] = {
                                "accession": acc,
                                "filed_date": str(f_item.get("filed_date", "")),
                                "period": period,
                                "supersedes": f_item.get("supersedes") or None,
                            }
            except Exception:
                logger.debug("Could not enrich filings from %s", detect_path)

        # 3. Fallback from context.ingest
        if context.ingest is not None:
            for recon in context.ingest.reconciliations:
                if recon.period == period and recon.accession not in by_acc:
                    by_acc[recon.accession] = {
                        "accession": recon.accession,
                        "filed_date": "",
                        "period": period,
                        "supersedes": None,
                    }

        return [by_acc[k] for k in sorted(by_acc.keys())]

    def run(self, context: PipelineContext) -> StageResult:
        """Execute report publication, CSV export generation, PR body rendering, and state gate."""
        period = context.target_period
        effective_replay = bool(self.replay or context.replay)
        effective_commit_state = bool(
            (self.commit_state or context.commit_state) and not effective_replay
        )

        period_dir = self.settings.reports_dir / period
        period_dir.mkdir(parents=True, exist_ok=True)

        # 1. Load FactsBundle
        facts = context.facts or context.artifacts.get("facts_bundle")
        if not isinstance(facts, FactsBundle):
            facts_path = period_dir / "facts.json"
            if facts_path.exists():
                facts = FactsBundle.from_file(facts_path)
                context.facts = facts
            else:
                raise StageError(f"PublishStage requires facts.json at {facts_path}")

        # 2. Load Draft
        draft_md: str | None = None
        if context.draft and context.draft.markdown:
            draft_md = context.draft.markdown
        else:
            draft_path = period_dir / "draft.md"
            if draft_path.exists():
                draft_md = draft_path.read_text(encoding="utf-8")
        if not draft_md:
            raise StageError(f"PublishStage requires draft.md for period {period}")

        # 3. Verify Validation Report (fail-closed)
        validator = ValidateStage(self.settings)
        val_report: ValidationReport = validator.validate_draft(draft_md, facts, period)
        val_json_path = period_dir / "validation_report.json"
        val_md_path = period_dir / "validation_report.md"
        val_json_path.write_text(
            json.dumps(val_report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        val_md_path.write_text(val_report.to_markdown(), encoding="utf-8")

        if val_report.status != "PASS":
            raise StageError(
                f"PublishStage refused: validation failed for {period} with "
                f"{len(val_report.issues)} defect(s)."
            )

        # 4. Ensure diff.md exists
        diff_path = period_dir / "diff.md"
        if diff_path.exists():
            diff_md = diff_path.read_text(encoding="utf-8")
        elif facts.diff_markdown:
            diff_md = facts.diff_markdown
            diff_path.write_text(diff_md, encoding="utf-8")
        else:
            diff_md = f"# Quarter-over-Quarter Comparison ({facts.baseline_period} to {period})\n"
            diff_path.write_text(diff_md, encoding="utf-8")

        # 5. Render final.md and final_cited.md
        final_clean_md = render_final_clean(draft_md, facts, replay=effective_replay)
        final_cited_md = render_final_cited(draft_md, replay=effective_replay)

        final_path = period_dir / "final.md"
        final_cited_path = period_dir / "final_cited.md"
        final_path.write_text(final_clean_md, encoding="utf-8")
        final_cited_path.write_text(final_cited_md, encoding="utf-8")

        # 6. Write exports/*.csv
        exports_dir = period_dir / "exports"
        export_paths = write_csv_exports(facts, exports_dir)

        # 7. Update run.json (scrubbed of any secrets)
        run_json_path = period_dir / "run.json"
        run_data: dict[str, Any] = {}
        if run_json_path.exists():
            try:
                run_data = json.loads(run_json_path.read_text(encoding="utf-8"))
            except Exception:
                run_data = {}

        stage_timings = dict(context.artifacts.get("stage_timings") or {})
        if stage_timings:
            run_data["stage_seconds"] = stage_timings
        elif not run_data.get("stage_seconds"):
            last_run_path = self.settings.state_dir / "last_run.json"
            if last_run_path.exists():
                try:
                    lr_data = json.loads(last_run_path.read_text(encoding="utf-8"))
                    if lr_data.get("target_period") == period:
                        extracted: dict[str, float] = {}
                        for st in lr_data.get("stages") or []:
                            if isinstance(st, dict) and st.get("stage_name"):
                                extracted[str(st["stage_name"])] = round(
                                    float(st.get("duration_ms", 0.0)) / 1000.0, 3
                                )
                        run_data["stage_seconds"] = extracted
                except Exception:
                    run_data.setdefault("stage_seconds", {})
            else:
                run_data.setdefault("stage_seconds", {})

        run_data["period"] = period
        run_data["replay"] = effective_replay
        run_data["mode_label"] = "replay" if effective_replay else "standard"
        run_data["state_committed"] = effective_commit_state

        published_files = [
            str(final_path),
            str(final_cited_path),
            str(val_md_path),
            str(diff_path),
            str(run_json_path),
        ]
        run_data["published_files"] = published_files
        run_data["export_files"] = [str(p) for p in export_paths]

        raw_run_json = json.dumps(run_data, indent=2, sort_keys=True) + "\n"
        safe_run_json = redact_text(raw_run_json)
        run_json_path.write_text(safe_run_json, encoding="utf-8")

        # 8. Build and write PR body
        pr_body_md = build_pr_body(
            period=period,
            diff_markdown=diff_md,
            validation_report=val_report,
            run_metadata=run_data,
            replay=effective_replay,
        )
        pr_body_path = period_dir / "pr_body.md"
        pr_body_path.write_text(pr_body_md, encoding="utf-8")
        published_files.append(str(pr_body_path))

        # 9. Commit state ONLY when --commit-state is set and --replay is False
        if effective_commit_state:
            state_path = self.settings.state_dir / "processed_filings.json"
            store = StateStore(state_path)
            filings_to_record = self._resolve_period_filings(period, context, facts)
            for item in filings_to_record:
                store.record_filing(
                    item["accession"],
                    period=item["period"],
                    filed_date=item["filed_date"],
                    supersedes=item["supersedes"],
                )
            store.save(dry_run=context.dry_run)
            published_files.append(str(state_path))
            logger.info(
                "Committed %d processed filings for %s to %s",
                len(filings_to_record),
                period,
                state_path,
            )
        else:
            logger.info(
                "PublishStage completed for %s without modifying state "
                "(commit_state=%s, replay=%s)",
                period,
                effective_commit_state,
                effective_replay,
            )

        pub_result = PublishResult(
            period=period,
            published_files=published_files,
            export_files=[str(p) for p in export_paths],
            state_committed=effective_commit_state,
            replay=effective_replay,
            pr_body_path=str(pr_body_path),
        )
        context.publish = pub_result
        context.artifacts["publish_result"] = pub_result

        return StageResult(
            status=StageStatus.SUCCEEDED,
            message=(
                f"Published {period} artifacts (replay={effective_replay}, "
                f"state_committed={effective_commit_state})"
            ),
            metadata={
                "export_files": pub_result.export_files,
                "pr_body_path": str(pr_body_path),
                "published_files": pub_result.published_files,
                "replay": effective_replay,
                "state_committed": effective_commit_state,
            },
        )
