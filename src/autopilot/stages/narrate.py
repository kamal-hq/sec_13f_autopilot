"""NarrateStage: renders deterministic or LLM-assisted research notes from `FactsBundle`.

Stage Contract
--------------
- **Stage Name**: ``narrate``
- **Input Contract**:
  - ``context.facts`` (:class:`autopilot.models.FactsBundle`): Verified quantitative dataset.
  - ``settings.prompts_dir`` (``Path``): Contains ``system.md``, ``note_template.md``, and
    ``style_guide.md``.
  - ``LLMProvider`` wrapped in :class:`autopilot.gateways.llm.BudgetGuard`: Selected via
    ``select_llm_provider(settings, wrap_with_budget_guard=True)``.
  - Optional prior ``context.validation`` errors if re-invoked during the validation retry loop.
- **Output Contract**:
  - Writes ``context.draft`` as a :class:`autopilot.models.NarrativeDraft` containing:
    - ``period`` (``str``): Target reporting period.
    - ``markdown`` (``str``): Drafted research note including the mandatory disclaimer.
    - ``llm_result`` (:class:`autopilot.models.LLMResult`): Provider metadata and token counts.
    - ``attempt`` (``int``): 1-based generation attempt index (up to ``max_validation_retries``).
  - Writes ``reports/<period>/draft.md`` deterministically.
- **Side Effects & Invariants**:
  - Pure deterministic output when using ``NullProvider`` (`--llm none`).
  - Every numeric figure in the generated draft is followed by its bracketed fact id citation,
    e.g. ``$18.67B [discretionary.GOOGL_GOOG.dollars_added]``.
  - Required limitation facts (``limit.*``) are always cited in the Limitations section.
  - Never logs prompt or response bodies; logs only status and token counts.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from autopilot.gateways.llm import (
    BudgetGuard,
    LLMProvider,
    LLMRateLimitError,
    NeedsManualInput,
    NullProvider,
    select_llm_provider,
)
from autopilot.models import (
    FactItem,
    FactsBundle,
    LLMResult,
    NarrativeDraft,
    PipelineContext,
    StageStatus,
)
from autopilot.settings import Settings
from autopilot.stages.base import Stage, StageError, StageResult
from autopilot.stages.validate import ValidateStage, ValidationReport

logger = logging.getLogger(__name__)

MANDATORY_DISCLAIMER = "*Not investment advice; AI-drafted, figures checked against filings data.*"


def _fmt_usd(val: int | float) -> str:
    """Format an integer or float as a signed currency string."""
    i_val = int(val)
    if i_val < 0:
        return f"-${abs(i_val):,}"
    return f"${i_val:,}"


def _fmt_pct(val: int | float) -> str:
    """Format a percentage with sign and 2 decimal places."""
    f_val = float(val)
    sign = "+" if f_val >= 0 else ""
    return f"{sign}{f_val:.2f}%"


def _fmt_num(val: int | float) -> str:
    """Format a number with comma separators."""
    if isinstance(val, int) or (isinstance(val, float) and val.is_integer()):
        return f"{int(val):,}"
    return f"{val:.2f}"


def _extract_company_and_sector(
    f_dol: FactItem | None,
    f_buy: FactItem | None,
    default_tok: str,
) -> tuple[str, str]:
    """Extract human-readable company and sector from FactItem display metadata."""
    comp = default_tok.replace("_", "/")
    sec = "Common Stock"
    for f in (f_dol, f_buy):
        if f and len(f.display) >= 7:
            c_candidate = str(f.display[5])
            s_candidate = str(f.display[6])
            if (
                c_candidate
                and not c_candidate.startswith("$")
                and not c_candidate.startswith("-")
                and not c_candidate.replace(".", "").isdigit()
            ):
                comp = c_candidate
            if (
                s_candidate
                and not s_candidate.startswith("$")
                and not s_candidate.startswith("-")
                and not s_candidate.replace(".", "").isdigit()
            ):
                sec = s_candidate
            break
    return comp, sec


def render_template_note(
    facts: FactsBundle,
    template_path: Path | None = None,
    provider: str = "none",
    llm_blocks: dict[str, str] | None = None,
) -> str:
    """Deterministically render `draft.md` from `FactsBundle` with every figure cited."""
    period = facts.period
    prev_period = facts.baseline_period

    # 1. Executive Summary
    aum_eq = facts.get_fact("market.aum_equity")
    aum_eq_val = int(aum_eq.value) if aum_eq else 0
    aum_eq_id = aum_eq.id if aum_eq else "market.aum_equity"

    aum_eq_prev = facts.get_fact("market.aum_equity_prev")
    aum_eq_prev_val = int(aum_eq_prev.value) if aum_eq_prev else 0
    aum_eq_prev_id = aum_eq_prev.id if aum_eq_prev else "market.aum_equity_prev"

    aum_eq_chg = facts.get_fact("market.aum_equity_change")
    aum_eq_chg_val = int(aum_eq_chg.value) if aum_eq_chg else (aum_eq_val - aum_eq_prev_val)
    aum_eq_chg_id = aum_eq_chg.id if aum_eq_chg else "market.aum_equity_change"

    aum_eq_pct = facts.get_fact("market.aum_equity_change_pct")
    aum_eq_pct_val = (
        float(aum_eq_pct.value)
        if aum_eq_pct
        else (round((aum_eq_chg_val / aum_eq_prev_val * 100.0), 2) if aum_eq_prev_val > 0 else 0.0)
    )
    aum_eq_pct_id = aum_eq_pct.id if aum_eq_pct else "market.aum_equity_change_pct"

    aum_opt = facts.get_fact("market.aum_with_options")
    aum_opt_val = int(aum_opt.value) if aum_opt else 0
    aum_opt_id = aum_opt.id if aum_opt else "market.aum_with_options"

    aum_opt_prev = facts.get_fact("market.aum_with_options_prev")
    aum_opt_prev_val = int(aum_opt_prev.value) if aum_opt_prev else 0
    aum_opt_prev_id = aum_opt_prev.id if aum_opt_prev else "market.aum_with_options_prev"

    aum_opt_chg = facts.get_fact("market.aum_with_options_change")
    aum_opt_chg_val = int(aum_opt_chg.value) if aum_opt_chg else (aum_opt_val - aum_opt_prev_val)
    aum_opt_chg_id = aum_opt_chg.id if aum_opt_chg else "market.aum_with_options_change"

    aum_opt_pct = facts.get_fact("market.aum_with_options_change_pct")
    aum_opt_pct_val = (
        float(aum_opt_pct.value)
        if aum_opt_pct
        else (
            round((aum_opt_chg_val / aum_opt_prev_val * 100.0), 2) if aum_opt_prev_val > 0 else 0.0
        )
    )
    aum_opt_pct_id = aum_opt_pct.id if aum_opt_pct else "market.aum_with_options_change_pct"

    opt_aum = facts.get_fact("market.options_aum")
    opt_aum_val = int(opt_aum.value) if opt_aum else 0
    opt_aum_id = opt_aum.id if opt_aum else "market.options_aum"

    opt_pct = facts.get_fact("market.options_pct_of_total")
    opt_pct_val = float(opt_pct.value) if opt_pct else 0.0
    opt_pct_id = opt_pct.id if opt_pct else "market.options_pct_of_total"

    sig_tot = facts.get_fact("market.signals_total")
    sig_tot_val = int(sig_tot.value) if sig_tot else 0
    sig_tot_id = sig_tot.id if sig_tot else "market.signals_total"

    sig_hf = facts.get_fact("market.signals_hedge_only")
    sig_hf_val = int(sig_hf.value) if sig_hf else 0
    sig_hf_id = sig_hf.id if sig_hf else "market.signals_hedge_only"

    sig_with_disc = facts.get_fact("market.signals_with_discretionary")
    sig_with_disc_val = int(sig_with_disc.value) if sig_with_disc else 0
    sig_with_disc_id = sig_with_disc.id if sig_with_disc else "market.signals_with_discretionary"

    sig_disc = facts.get_fact("market.signals_discretionary")
    sig_disc_val = int(sig_disc.value) if sig_disc else 0
    sig_disc_id = sig_disc.id if sig_disc else "market.signals_discretionary"

    sig_new_sample = facts.get_fact("market.signals_new_to_sample")
    sig_new_sample_val = int(sig_new_sample.value) if sig_new_sample else 0
    sig_new_sample_id = sig_new_sample.id if sig_new_sample else "market.signals_new_to_sample"

    exec_lines = [
        f"- **Reporting Period**: {period} (comparison baseline: {prev_period})",
        (
            "- **Equity-only AUM (excludes options; includes ETFs)**: "
            f"`{_fmt_usd(aum_eq_val)} [{aum_eq_id}]` "
            f"(prior: `{_fmt_usd(aum_eq_prev_val)} [{aum_eq_prev_id}]`, "
            f"change: `{_fmt_usd(aum_eq_chg_val)} [{aum_eq_chg_id}]` / "
            f"`{_fmt_pct(aum_eq_pct_val)} [{aum_eq_pct_id}]`)"
        ),
        (
            "- **Total 13F AUM (Incl. Options)**: "
            f"`{_fmt_usd(aum_opt_val)} [{aum_opt_id}]` "
            f"(prior: `{_fmt_usd(aum_opt_prev_val)} [{aum_opt_prev_id}]`, "
            f"change: `{_fmt_usd(aum_opt_chg_val)} [{aum_opt_chg_id}]` / "
            f"`{_fmt_pct(aum_opt_pct_val)} [{aum_opt_pct_id}]`)"
        ),
        (
            f"- **Options Derivative AUM**: `{_fmt_usd(opt_aum_val)} [{opt_aum_id}]` "
            f"({opt_pct_val:.2f}% [{opt_pct_id}] of gross portfolio assets)"
        ),
        (
            "- **Broad Convergence Signals (4+ funds buying)**: "
            f"`{_fmt_num(sig_tot_val)} [{sig_tot_id}]` "
            f"(`{_fmt_num(sig_hf_val)} [{sig_hf_id}]` hedge-fund-only, "
            f"`{_fmt_num(sig_with_disc_val)} [{sig_with_disc_id}]` "
            "including discretionary allocators)"
        ),
        (
            "- **Discretionary Convergence Signals (2+ of 3 discretionary funds)**: "
            f"`{_fmt_num(sig_disc_val)} [{sig_disc_id}]`"
        ),
        (
            "- **New-to-Sample Signals (3+ funds buying, zero prior holders)**: "
            f"`{_fmt_num(sig_new_sample_val)} [{sig_new_sample_id}]`"
        ),
    ]

    # 2. Data and Coverage
    funds_filed = facts.get_fact("coverage.funds_filed")
    recon_pass = facts.get_fact("coverage.reconciliations_passed")
    recon_tot = facts.get_fact("coverage.reconciliations_total")
    amend_fact = facts.get_fact("coverage.amendments_applied")

    f_filed_num = _fmt_num(funds_filed.value if funds_filed else 0)
    r_pass_num = _fmt_num(recon_pass.value if recon_pass else 0)
    r_tot_num = _fmt_num(recon_tot.value if recon_tot else 0)
    amend_num = _fmt_num(amend_fact.value if amend_fact else 0)

    cov_lines = [
        f"- **Tracked Institutions Filed**: `{f_filed_num} [coverage.funds_filed]` institutions",
        (
            "- **Cover-Page Value Reconciliation**: "
            f"`{r_pass_num} [coverage.reconciliations_passed]` "
            f"of `{r_tot_num} [coverage.reconciliations_total]` filings passed "
            f"(all reconciled [coverage.all_reconciled])"
        ),
        f"- **Form 13F-HR/A Restatements Applied**: `{amend_num} [coverage.amendments_applied]`",
    ]

    # 3. Broad Convergence
    broad_tickers: dict[str, dict[str, Any]] = {}
    for f in facts.facts:
        if f.id.startswith("signal.") and not f.id.startswith("signal.summary"):
            parts = f.id.split(".")
            if len(parts) >= 3:
                tok = parts[1]
                field_name = parts[2]
                broad_tickers.setdefault(tok, {})[field_name] = f

    # Sort broad signals by dollars_added descending (top 10)
    sorted_broad = sorted(
        broad_tickers.items(),
        key=lambda item: (
            -int(item[1].get("dollars_added").value if item[1].get("dollars_added") else 0),
            item[0],
        ),
    )[:10]

    broad_lines: list[str] = []
    if sorted_broad:
        for tok, fmap in sorted_broad:
            f_buy = fmap.get("funds_buying")
            f_dol = fmap.get("dollars_added")
            f_hf = fmap.get("hedge_fund_buyers")
            f_disc = fmap.get("discretionary_buyers")

            comp_name, sector_name = _extract_company_and_sector(f_dol, f_buy, tok)

            dollars_val = int(f_dol.value) if f_dol else 0
            funds_val = int(f_buy.value) if f_buy else 0
            hf_val = int(f_hf.value) if f_hf else 0
            disc_val = int(f_disc.value) if f_disc else 0

            broad_lines.append(
                f"- **`{tok.replace('_', '/')}`** ({comp_name} — {sector_name}): "
                f"`{_fmt_num(funds_val)} [signal.{tok}.funds_buying]` funds buying, "
                f"`{_fmt_usd(dollars_val)} [signal.{tok}.dollars_added]` estimated dollars added "
                f"(Hedge Fund: `{_fmt_num(hf_val)} [signal.{tok}.hedge_fund_buyers]`, "
                f"Discretionary: `{_fmt_num(disc_val)} [signal.{tok}.discretionary_buyers]`)"
            )
    else:
        broad_lines.append(
            f"- No broad convergence signals detected in {period} with 4+ institutional buyers."
        )

    # 4. Discretionary Convergence
    disc_tickers: dict[str, dict[str, Any]] = {}
    for f in facts.facts:
        if f.id.startswith("discretionary."):
            parts = f.id.split(".")
            if len(parts) >= 3:
                tok = parts[1]
                field_name = parts[2]
                disc_tickers.setdefault(tok, {})[field_name] = f

    sorted_disc = sorted(
        disc_tickers.items(),
        key=lambda item: (
            -int(item[1].get("dollars_added").value if item[1].get("dollars_added") else 0),
            item[0],
        ),
    )

    disc_lines: list[str] = []
    if sorted_disc:
        for tok, fmap in sorted_disc:
            f_buy = fmap.get("funds_buying")
            f_dol = fmap.get("dollars_added")

            comp_name, sector_name = _extract_company_and_sector(f_dol, f_buy, tok)

            dollars_val = int(f_dol.value) if f_dol else 0
            funds_val = int(f_buy.value) if f_buy else 0

            # Fund breakdown
            fund_chunks: list[str] = []
            for fund_suffix, fund_label in [
                ("BERKSHIRE_HATHAWAY", "Berkshire Hathaway"),
                ("CALPERS", "CalPERS"),
                ("TEMASEK_HOLDINGS", "Temasek Holdings"),
            ]:
                fund_fact = fmap.get(fund_suffix) or facts.get_fact(
                    f"discretionary.{tok}.{fund_suffix}.dollars_added"
                )
                if fund_fact:
                    fund_chunks.append(
                        f"{fund_label} `{_fmt_usd(int(fund_fact.value))} [{fund_fact.id}]`"
                    )

            fund_breakdown_str = f" [{', '.join(fund_chunks)}]" if fund_chunks else ""

            disc_lines.append(
                f"- **`{tok.replace('_', '/')}`** ({comp_name} — {sector_name}): "
                f"`{_fmt_num(funds_val)} [discretionary.{tok}.funds_buying]` "
                "discretionary funds buying, "
                f"`{_fmt_usd(dollars_val)} [discretionary.{tok}.dollars_added]` "
                f"estimated dollars added{fund_breakdown_str}"
            )
    else:
        disc_lines.append(
            f"- No discretionary convergence signals detected in {period} "
            "across tracked allocators."
        )

    # 5. New to the Sample
    new_sample_tickers: dict[str, dict[str, Any]] = {}
    for f in facts.facts:
        if f.id.startswith("new_to_sample."):
            parts = f.id.split(".")
            if len(parts) >= 3:
                tok = parts[1]
                field_name = parts[2]
                new_sample_tickers.setdefault(tok, {})[field_name] = f

    sorted_new_sample = sorted(
        new_sample_tickers.items(),
        key=lambda item: (
            -int(item[1].get("dollars_added").value if item[1].get("dollars_added") else 0),
            item[0],
        ),
    )

    new_sample_lines: list[str] = []
    if sorted_new_sample:
        for tok, fmap in sorted_new_sample:
            f_buy = fmap.get("funds_buying")
            f_dol = fmap.get("dollars_added")

            comp_name, sector_name = _extract_company_and_sector(f_dol, f_buy, tok)

            dollars_val = int(f_dol.value) if f_dol else 0
            funds_val = int(f_buy.value) if f_buy else 0

            new_sample_lines.append(
                f"- **`{tok.replace('_', '/')}`** ({comp_name} — {sector_name}): "
                f"`{_fmt_num(funds_val)} [new_to_sample.{tok}.funds_buying]` funds initiating, "
                f"`{_fmt_usd(dollars_val)} [new_to_sample.{tok}.dollars_added]` "
                "estimated dollars added"
            )
    else:
        new_sample_lines.append(f"- No new-to-sample convergence signals initiated in {period}.")

    # 6. What Changed Since Last Quarter
    diff_unavail = facts.get_fact("diff.unavailable")
    diff_lines: list[str] = []
    if diff_unavail:
        reason_str = (
            diff_unavail.display[1]
            if len(diff_unavail.display) >= 2
            else (
                diff_unavail.display[0]
                if diff_unavail.display
                else f"lookback period prior to {prev_period} not available"
            )
        )
        diff_lines.append(
            f"- **Prior-Quarter Signal Comparison**: Unavailable [diff.unavailable]: {reason_str}"
        )
    else:
        sig_persisted = facts.get_fact("diff.signals_persisted")
        sig_new = facts.get_fact("diff.signals_new")
        sig_dropped = facts.get_fact("diff.signals_dropped")

        persisted_val = int(sig_persisted.value) if sig_persisted else 0
        new_val = int(sig_new.value) if sig_new else 0
        dropped_val = int(sig_dropped.value) if sig_dropped else 0

        persisted_list_str = (
            sig_persisted.display[1]
            if (sig_persisted and len(sig_persisted.display) >= 2)
            else (sig_persisted.display[0] if (sig_persisted and sig_persisted.display) else "none")
        )
        new_list_str = (
            sig_new.display[1]
            if (sig_new and len(sig_new.display) >= 2)
            else (sig_new.display[0] if (sig_new and sig_new.display) else "none")
        )
        dropped_list_str = (
            sig_dropped.display[1]
            if (sig_dropped and len(sig_dropped.display) >= 2)
            else (sig_dropped.display[0] if (sig_dropped and sig_dropped.display) else "none")
        )

        diff_lines.extend(
            [
                (
                    "- **Persisted Broad Signals**: "
                    f"`{_fmt_num(persisted_val)} [diff.signals_persisted]` "
                    f"({persisted_list_str})"
                ),
                (
                    "- **New Broad Signals**: "
                    f"`{_fmt_num(new_val)} [diff.signals_new]` "
                    f"({new_list_str})"
                ),
                (
                    "- **Dropped Broad Signals**: "
                    f"`{_fmt_num(dropped_val)} [diff.signals_dropped]` "
                    f"({dropped_list_str})"
                ),
            ]
        )

    # Position movers: separate Operating Companies and ETFs
    op_movers: list[tuple[str, int, int, str, str]] = []
    etf_movers: list[tuple[str, int, int, str, str]] = []

    for f in facts.facts:
        if f.id.startswith("diff.mover.") and f.id.endswith(".value_change"):
            tok = f.id[len("diff.mover.") : -len(".value_change")]
            val_change = int(f.value)
            is_etf_fact = facts.get_fact(f"diff.mover.{tok}.is_etf")
            is_etf = int(is_etf_fact.value) if is_etf_fact else 0

            comp_str = f.display[6] if len(f.display) >= 7 else tok
            ticker_str = f.display[5] if len(f.display) >= 6 else tok

            if is_etf == 1:
                etf_movers.append((tok, val_change, is_etf, ticker_str, comp_str))
            else:
                op_movers.append((tok, val_change, is_etf, ticker_str, comp_str))

    # Sort movers by absolute value change descending (top 5 each)
    op_movers.sort(key=lambda m: -abs(m[1]))
    etf_movers.sort(key=lambda m: -abs(m[1]))

    if op_movers:
        diff_lines.append("\n### Operating Company Position Movers")
        for tok, val_change, _is_etf, tkr, comp in op_movers[:5]:
            diff_lines.append(
                f"- **`{tkr}`** ({comp}): `{_fmt_usd(val_change)} [diff.mover.{tok}.value_change]` "
                f"[diff.mover.{tok}.is_etf]"
            )

    if etf_movers:
        diff_lines.append("\n### ETF Position Movers")
        for tok, val_change, _is_etf, tkr, comp in etf_movers[:5]:
            diff_lines.append(
                f"- **`{tkr}`** ({comp}): `{_fmt_usd(val_change)} [diff.mover.{tok}.value_change]` "
                f"[diff.mover.{tok}.is_etf]"
            )

    # 7. Sector View
    sector_facts: dict[str, dict[str, Any]] = {}
    for f in facts.facts:
        if f.id.startswith("sector.") and not f.id.endswith(".note"):
            parts = f.id.split(".")
            if len(parts) >= 3:
                tok = parts[1]
                field_name = parts[2]
                sector_facts.setdefault(tok, {})[field_name] = f

    sorted_sectors = sorted(
        sector_facts.items(),
        key=lambda item: (
            -int(item[1].get("value_usd").value if item[1].get("value_usd") else 0),
            item[0],
        ),
    )

    sector_lines: list[str] = []
    for tok, smap in sorted_sectors:
        f_val = smap.get("value_usd")
        f_cnt = smap.get("fund_count")
        f_prev = smap.get("value_prev_usd")
        f_chg = smap.get("value_change")
        f_pct = smap.get("value_change_pct")

        sector_title = (
            f_val.display[5]
            if (f_val and len(f_val.display) >= 6)
            else (f_val.display[0] if (f_val and f_val.display) else tok.replace("_", " "))
        )
        v_curr = int(f_val.value) if f_val else 0
        cnt_val = int(f_cnt.value) if f_cnt else 0

        extra_parts: list[str] = []
        if f_prev and f_prev.value is not None:
            extra_parts.append(
                f"prior: `{_fmt_usd(int(f_prev.value))} [sector.{tok}.value_prev_usd]`"
            )
        if f_chg and f_chg.value is not None:
            extra_parts.append(
                f"change: `{_fmt_usd(int(f_chg.value))} [sector.{tok}.value_change]`"
            )
        if f_pct and f_pct.value is not None:
            extra_parts.append(f"`{_fmt_pct(float(f_pct.value))} [sector.{tok}.value_change_pct]`")

        extra_str = f" ({', '.join(extra_parts)})" if extra_parts else ""

        sector_lines.append(
            f"- **{sector_title}**: `{_fmt_usd(v_curr)} [sector.{tok}.value_usd]`"
            f"{extra_str}, `{_fmt_num(cnt_val)} [sector.{tok}.fund_count]` funds"
        )
        if tok == "Unknown":
            note_fact = facts.get_fact("sector.Unknown.note")
            if note_fact:
                sector_lines.append(
                    "  - *Sector Context*: mostly ETFs and trusts without an operating-company "
                    "SIC code [sector.Unknown.note]"
                )

    # 8. Data Quality
    dq_lines: list[str] = []
    # Confirmed splits
    confirmed_splits = [
        f for f in facts.facts if f.id.startswith("quality.split.") and f.id.endswith(".confirmed")
    ]
    for f in confirmed_splits:
        tok = f.id[len("quality.split.") : -len(".confirmed")]
        comp_split = (
            f.display[4]
            if len(f.display) >= 5 and f.display[4]
            else (f.display[5] if len(f.display) >= 6 else tok)
        )
        dq_lines.append(
            f"- **Confirmed Stock Split**: `{tok}` ({comp_split}) applied "
            f"`{_fmt_num(float(f.value))} [{f.id}]`-for-1 multiplier"
        )

    # Unconfirmed splits
    unconfirmed_splits = [
        f
        for f in facts.facts
        if f.id.startswith("quality.split.") and f.id.endswith(".unconfirmed")
    ]
    for f in unconfirmed_splits:
        tok = f.id[len("quality.split.") : -len(".unconfirmed")]
        comp_split = (
            f.display[4]
            if len(f.display) >= 5 and f.display[4]
            else (f.display[5] if len(f.display) >= 6 else tok)
        )
        dq_lines.append(
            f"- **Unconfirmed Split Candidate**: `{tok}` ({comp_split}) flagged "
            f"`{_fmt_num(float(f.value))} [{f.id}]`-for-1 candidate"
        )

    # Restatements applied
    restatements = [
        f
        for f in facts.facts
        if f.id.startswith("quality.restatement.") and not f.id.endswith(".superseded_total_usd")
    ]
    for f in restatements:
        tok = f.id[len("quality.restatement.") :]
        fund_label = tok.replace("_", " ").title()
        restated_fact = facts.get_fact(f"fund.{tok}.aum_with_options")
        if restated_fact:
            dq_lines.append(
                f"- **Restatement Applied**: {fund_label} restated filing; total 13F AUM "
                f"restated to `{_fmt_usd(int(restated_fact.value))} [{restated_fact.id}]`"
            )
        else:
            dq_lines.append(
                f"- **Restatement Applied**: {fund_label} restated filing "
                f"(`{_fmt_num(int(f.value))} [{f.id}]` applied)"
            )

    if not dq_lines:
        dq_lines.append(
            "- **Data Quality Audits**: All "
            f"`{_fmt_num(recon_pass.value if recon_pass else 0)} "
            "[coverage.reconciliations_passed]` "
            "cover-page reconciliations reconciled to the dollar with zero anomalous "
            "position restatements."
        )

    # 9. Limitations (Must cite all required limitation facts)
    lim_lines = [
        (
            "- **Long-Only Scope**: Form 13F filings reflect exclusively long US equity positions "
            "and exclude short positions, cash holdings, foreign exchange, commodities, "
            "and unlisted securities [limit.long_only]."
        ),
        (
            "- **45-Day Reporting Lag**: Filings report quarter-end positions and are submitted "
            "up to 45 calendar days after period close, meaning observed positions may have been "
            "modified prior to publication [limit.filing_lag_45d]."
        ),
        (
            "- **No Forward Return Test**: Smart Money Convergence identifies historical co-buying "
            "patterns across institutional allocators; it does not constitute a forward return "
            "backtest or guarantee of future outperformance [limit.no_forward_return_test]."
        ),
        (
            "- **Hedge-Only Signals May Reflect Hedging**: Signals driven exclusively by "
            "hedge funds may represent arbitrage legs, statistical arbitrage baskets, or "
            "delta hedges against short positions rather than unhedged directional conviction "
            "[limit.hedge_only_may_be_hedging]."
        ),
        (
            "- **Small Discretionary Sample**: The discretionary cohort comprises 3 institutional "
            "allocators (Berkshire Hathaway, CalPERS, Temasek Holdings); consensus across 2 funds "
            "represents a small sample size that warrants qualitative verification "
            "[limit.small_discretionary_sample]."
        ),
        (
            "- **Estimated Dollars Added**: Dollars added figures are estimated as shares added "
            "multiplied by quarter-end closing price; actual purchase prices vary across the "
            "quarter [limit.dollars_added_estimate]."
        ),
    ]

    if provider in ("null", "none", None):
        footer_text = "*Generated deterministically from filings data; not investment advice.*"
    else:
        footer_text = (
            "*Drafted with AI; every figure validated against filings data; "
            "reviewed by a human; not investment advice.*"
        )

    # Assemble document
    sections: dict[str, str] = {
        "period": period,
        "executive_summary": "\n".join(exec_lines),
        "data_and_coverage": "\n".join(cov_lines),
        "broad_convergence": "\n".join(broad_lines),
        "discretionary_convergence": "\n".join(disc_lines),
        "new_to_the_sample": "\n".join(new_sample_lines),
        "what_changed_since_last_quarter": "\n".join(diff_lines),
        "sector_view": "\n".join(sector_lines),
        "data_quality": "\n".join(dq_lines),
        "limitations": "\n".join(lim_lines),
        "footer": footer_text,
    }

    if llm_blocks:
        for sec_key, prose in llm_blocks.items():
            if sec_key in sections and sec_key != "limitations" and prose.strip():
                existing = sections[sec_key]
                sections[sec_key] = f"{prose.strip()}\n\n{existing}"

    if template_path and template_path.exists():
        template_content = template_path.read_text(encoding="utf-8")
        rendered = template_content
        for key, val in sections.items():
            rendered = rendered.replace(f"{{{{ {key} }}}}", val)
            rendered = rendered.replace(f"{{{{{key}}}}}", val)
    else:
        # Default layout if template file is absent
        rendered = (
            f"# Quarterly 13F Research Note ({period})\n\n"
            f"## Executive Summary\n\n{sections['executive_summary']}\n\n"
            f"## Data and Coverage\n\n{sections['data_and_coverage']}\n\n"
            f"## Broad Convergence\n\n{sections['broad_convergence']}\n\n"
            f"## Discretionary Convergence\n\n{sections['discretionary_convergence']}\n\n"
            f"## New to the Sample\n\n{sections['new_to_the_sample']}\n\n"
            "## What Changed Since Last Quarter\n\n"
            f"{sections['what_changed_since_last_quarter']}\n\n"
            f"## Sector View\n\n{sections['sector_view']}\n\n"
            f"## Data Quality\n\n{sections['data_quality']}\n\n"
            f"## Limitations\n\n{sections['limitations']}\n\n"
            f"---\n{footer_text}\n"
        )

    # Normalize newlines
    lines = [line.rstrip() for line in rendered.splitlines()]
    return "\n".join(lines).strip() + "\n"


def render_clean(draft: str) -> str:
    """Strip bracketed fact citation tags to produce clean Markdown for human reading."""
    cleaned = re.sub(r"\s*\[([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)\](?!\()", "", draft)
    cleaned = re.sub(r" +([.,;:)\]])", r"\1", cleaned)
    return cleaned


def _clean_fact_label(fid: str) -> str:
    """Generate concise, human-readable semantic label for fact id."""
    if fid.startswith("market."):
        tok = fid[len("market.") :]
        return f"Market {tok.replace('_', ' ').title()}"
    if fid.startswith("coverage."):
        tok = fid[len("coverage.") :]
        return f"Coverage {tok.replace('_', ' ').title()}"
    if fid.startswith("signal."):
        parts = fid.split(".")
        return f"Broad Signal {parts[1]} {parts[2].replace('_', ' ')}"
    if fid.startswith("discretionary."):
        parts = fid.split(".")
        suffix = " ".join(parts[2:])
        return f"Discretionary {parts[1]} {suffix.replace('_', ' ')}"
    if fid.startswith("new_to_sample."):
        parts = fid.split(".")
        return f"New to Sample {parts[1]} {parts[2].replace('_', ' ')}"
    if fid.startswith("diff.mover."):
        parts = fid.split(".")
        return f"Mover {parts[2]} {parts[3].replace('_', ' ')}"
    if fid.startswith("diff."):
        tok = fid[len("diff.") :]
        return f"Diff {tok.replace('_', ' ').title()}"
    if fid.startswith("sector."):
        parts = fid.split(".")
        return f"Sector {parts[1]} {parts[2].replace('_', ' ')}"
    if fid.startswith("quality.split."):
        parts = fid.split(".")
        return f"Split {parts[2]} {parts[3]}"
    if fid.startswith("quality.restatement."):
        tok = fid[len("quality.restatement.") :]
        return f"Restatement {tok.replace('_', ' ').title()}"
    if fid.startswith("fund."):
        parts = fid.split(".")
        return f"Fund {parts[1]} {parts[2].replace('_', ' ')}"
    return fid


def _clean_fact_displays(displays: tuple[str, ...] | list[str]) -> str:
    """Filter displays to clean numeric/currency representations, stripping paths and XML."""
    cleaned: list[str] = []
    for d in displays:
        s = str(d).strip()
        if not s:
            continue
        # Strip paths, XML, or excessively long dumps
        if "/" in s or "\\" in s or "<" in s or ">" in s or len(s) > 40:
            continue
        cleaned.append(s)
    return ", ".join(cleaned[:4]) if cleaned else "N/A"


def build_compact_fact_sheet(facts: FactsBundle) -> str:
    """Build a compact (id | label | display) table for narrative commentary blocks."""
    relevant_prefixes = (
        "market.",
        "coverage.funds_filed",
        "coverage.reconciliations_passed",
        "coverage.reconciliations_total",
        "coverage.all_reconciled",
        "coverage.amendments_applied",
        "signal.",
        "discretionary.",
        "new_to_sample.",
        "diff.",
        "sector.",
        "quality.",
        "fund.",
    )

    rows: list[str] = [
        "| Fact ID | Semantic Label | Acceptable Display Values |",
        "| :--- | :--- | :--- |",
    ]

    for f in facts.facts:
        fid = f.id
        if f.superseded or fid.startswith("limit.") or fid.startswith("coverage.fund."):
            continue
        if any(fid.startswith(p) for p in relevant_prefixes):
            lbl = _clean_fact_label(fid)
            disp = _clean_fact_displays(f.display)
            rows.append(f"| `{fid}` | {lbl} | {disp} |")

    return "\n".join(rows)


def build_narrative_prompt(
    facts: FactsBundle,
    prompts_dir: Path | str,
    period: str,
    *,
    style_examples_dir: Path | str | None = None,
) -> str:
    """Construct initial prompt for LLM narrative blocks (with at most 1 style example)."""
    from autopilot.metrics import load_one_style_example

    pdir = Path(prompts_dir)
    sys_path = pdir / "system.md"
    style_path = pdir / "style_guide.md"

    sys_text = sys_path.read_text(encoding="utf-8") if sys_path.exists() else ""
    style_text = style_path.read_text(encoding="utf-8") if style_path.exists() else ""

    fact_sheet = build_compact_fact_sheet(facts)
    style_example_block = ""
    if style_examples_dir is not None:
        excerpt = load_one_style_example(style_examples_dir)
        if excerpt:
            style_example_block = f"## Approved Style Example (Reference Only):\n\n{excerpt}\n\n"

    prompt = (
        f"{sys_text}\n\n"
        f"---\n\n"
        f"{style_text}\n\n"
        f"---\n\n"
        f"# Task: Draft Quarterly 13F Narrative Commentary ({period})\n\n"
        f"Draft concise narrative commentary for the quarterly 13F research note for period "
        f"{period}.\n"
        f"You write ONLY narrative prose paragraphs under the exact headings below.\n"
        f"Do NOT write bulleted lists, tables, or limitations (managed deterministically).\n\n"
        f"## Mandatory Rules:\n"
        f"1. Use ONLY facts listed in the compact fact sheet below. Never invent numbers.\n"
        f"2. Every numeric figure, count, percentage, or dollar amount MUST be cited with its "
        f"bracketed fact id: `<figure> [<fact_id>]`.\n"
        f"3. Non-figure facts must be cited as `[<fact_id>]` without figures.\n"
        f"4. No forward-looking predictions, trade advice, or promotional claims.\n"
        f"5. No causal claims (avoid 'because', 'due to', 'driven by', 'as a result of').\n"
        f"6. Strictly NO URLs, NO HTML tags, NO markdown links, and NO code fences.\n"
        f"7. Length cap: strictly under 150 words per narrative block.\n"
        f"8. Use the exact canonical section headings:\n"
        f"   - ## Executive Summary\n"
        f"   - ## Data and Coverage\n"
        f"   - ## Broad Convergence\n"
        f"   - ## Discretionary Convergence\n"
        f"   - ## New to the Sample\n"
        f"   - ## What Changed Since Last Quarter\n"
        f"   - ## Sector View\n"
        f"   - ## Data Quality\n\n"
        f"{style_example_block}"
        f"## Compact Fact Sheet ({period})\n\n"
        f"{fact_sheet}\n"
    )
    return prompt


def build_repair_prompt(
    facts: FactsBundle,
    prompts_dir: Path | str,
    period: str,
    previous_response: str,
    report: ValidationReport,
    attempt: int,
    max_attempts: int,
) -> str:
    """Construct repair prompt containing exact validation findings."""
    fact_sheet = build_compact_fact_sheet(facts)

    defect_rows: list[str] = []
    for issue in report.issues:
        snip = issue.snippet.replace("|", "\\|").replace("\n", " ")[:60]
        exp = issue.expected.replace("|", "\\|").replace("\n", " ")
        defect_rows.append(f"| {issue.rule_id} | Line {issue.line} | `{snip}` | {exp} |")

    defect_table = (
        "| Rule ID | Location | Defect Snippet | Expected / Instruction |\n"
        "| :--- | :--- | :--- | :--- |\n" + "\n".join(defect_rows)
    )

    prompt = (
        f"# Validation Repair Request (Attempt {attempt + 1} of {max_attempts})\n\n"
        f"Your previous narrative submission for {period} failed deterministic validation with "
        f"{len(report.issues)} defect(s):\n\n"
        f"{defect_table}\n\n"
        f"## Previous Response with Issues:\n"
        f"{previous_response}\n\n"
        f"## Mandatory Correction Instructions:\n"
        f"1. Fix all defects identified in the table above.\n"
        f"2. Ensure every figure cited in your text is followed by its bracketed fact id "
        f"citation: `<figure> [<fact_id>]`.\n"
        f"3. Non-figure facts must be cited as `[<fact_id>]` without figures.\n"
        f"4. Do NOT include URLs, HTML tags, markdown links, or code fences.\n"
        f"5. Keep each narrative block strictly under 150 words.\n"
        f"6. Use the exact canonical section headings:\n"
        f"   - ## Executive Summary\n"
        f"   - ## Data and Coverage\n"
        f"   - ## Broad Convergence\n"
        f"   - ## Discretionary Convergence\n"
        f"   - ## New to the Sample\n"
        f"   - ## What Changed Since Last Quarter\n"
        f"   - ## Sector View\n"
        f"   - ## Data Quality\n\n"
        f"## Compact Fact Sheet (Reference):\n\n"
        f"{fact_sheet}\n"
    )
    return prompt


def parse_llm_blocks(llm_text: str) -> dict[str, str]:
    """Parse section blocks from LLM markdown response by canonical section headings."""
    if not llm_text or not llm_text.strip():
        return {}

    lines = llm_text.splitlines()
    blocks: dict[str, list[str]] = {}
    current_key: str | None = None

    heading_patterns = [
        ("executive", "executive_summary"),
        ("coverage", "data_and_coverage"),
        ("broad", "broad_convergence"),
        ("discretionary", "discretionary_convergence"),
        ("new to", "new_to_the_sample"),
        ("changed", "what_changed_since_last_quarter"),
        ("sector", "sector_view"),
        ("quality", "data_quality"),
    ]

    has_headings = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("# "):
            heading_text = stripped.lstrip("#").strip().lower()
            matched_key = None
            for pattern, key in heading_patterns:
                if pattern in heading_text:
                    matched_key = key
                    break
            if matched_key:
                has_headings = True
                current_key = matched_key
                blocks.setdefault(current_key, [])
                continue
        if current_key:
            blocks.setdefault(current_key, []).append(line)

    if not has_headings:
        clean_text = llm_text.strip()
        return {"executive_summary": clean_text} if clean_text else {}

    result: dict[str, str] = {}
    for k, line_list in blocks.items():
        text = "\n".join(line_list).strip()
        if text:
            result[k] = text
    return result


def merge_llm_blocks_into_skeleton(
    facts: FactsBundle,
    llm_blocks: dict[str, str],
    template_path: Path | None = None,
    provider: str = "none",
) -> str:
    """Merge LLM narrative blocks into deterministic note template."""
    return render_template_note(
        facts=facts,
        template_path=template_path,
        provider=provider,
        llm_blocks=llm_blocks,
    )


class NarrateStage(Stage):
    """Drafts the quarterly research note from `FactsBundle` deterministically or via LLM."""

    name: str = "narrate"

    def __init__(
        self,
        settings: Settings | None = None,
        provider: LLMProvider | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._provider = provider
        self.max_attempts = (
            max_attempts if max_attempts is not None else self.settings.max_validation_retries
        )

    def run(self, context: PipelineContext) -> StageResult:
        """Execute narrative generation stage contract."""
        period = context.target_period
        facts = context.facts or context.artifacts.get("facts_bundle")

        if not isinstance(facts, FactsBundle):
            facts_path = self.settings.reports_dir / period / "facts.json"
            if facts_path.exists():
                logger.info("Loading FactsBundle from %s for NarrateStage", facts_path)
                facts = FactsBundle.from_file(facts_path)
                context.facts = facts
                context.artifacts["facts_bundle"] = facts
            else:
                raise StageError(
                    f"NarrateStage requires a verified FactsBundle; "
                    f"neither context.facts nor {facts_path} was found."
                )

        provider = self._provider
        if provider is None:
            provider = select_llm_provider(
                self.settings,
                period=period,
                wrap_with_budget_guard=True,
            )
        elif not isinstance(provider, BudgetGuard | NullProvider):
            provider = BudgetGuard(
                provider,
                max_calls_per_run=self.settings.llm_max_calls_per_run,
                max_output_tokens=self.settings.llm_max_output_tokens,
                timeout_seconds=self.settings.llm_timeout_seconds,
                llm_disabled=self.settings.llm_disabled,
            )

        provider_name = provider.provider_name if hasattr(provider, "provider_name") else "none"
        template_path = self.settings.prompts_dir / "note_template.md"
        deterministic_draft = render_template_note(
            facts,
            template_path=template_path,
            provider="none",
        )

        is_null_provider = (
            provider_name in ("null", "none")
            or isinstance(provider, NullProvider)
            or (isinstance(provider, BudgetGuard) and isinstance(provider.provider, NullProvider))
        )

        recorded_attempts = 0
        total_prompt_tokens = 0
        total_output_tokens = 0
        total_latency_ms = 0.0
        rule_failures: dict[str, int] = {}
        llm_status = "deterministic"
        final_draft = deterministic_draft
        last_llm_result: LLMResult | None = None

        if is_null_provider:
            final_draft = deterministic_draft
            llm_status = "deterministic"
            last_llm_result = LLMResult(
                text=deterministic_draft,
                provider="null",
                model="deterministic-template",
                prompt_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=0.0,
                finish_reason="STOP",
            )
        else:
            attempts_dir = self.settings.reports_dir / period / "attempts"
            attempts_dir.mkdir(parents=True, exist_ok=True)
            current_prompt = build_narrative_prompt(facts, self.settings.prompts_dir, period)
            validator = ValidateStage(self.settings)

            for attempt_idx in range(1, self.max_attempts + 1):
                recorded_attempts = attempt_idx
                prompt_file = attempts_dir / f"attempt_{attempt_idx}_prompt.md"
                prompt_file.write_text(current_prompt, encoding="utf-8")

                try:
                    llm_res = provider.generate(prompt=current_prompt)
                except LLMRateLimitError as exc:
                    logger.warning(
                        "Upstream rate limit (429) hit: %s. Falling back to deterministic draft.",
                        exc,
                    )
                    final_draft = deterministic_draft
                    llm_status = "quota_exhaustion_fallback"
                    last_llm_result = LLMResult(
                        text=deterministic_draft,
                        provider=provider_name,
                        model=getattr(provider, "model", "unknown"),
                        prompt_tokens=total_prompt_tokens,
                        output_tokens=total_output_tokens,
                        total_tokens=total_prompt_tokens + total_output_tokens,
                        latency_ms=total_latency_ms,
                        finish_reason="RATE_LIMIT",
                    )
                    break
                except NeedsManualInput as exc:
                    logger.info("Human input required for manual mode: %s", exc)
                    raise

                last_llm_result = llm_res
                total_prompt_tokens += llm_res.prompt_tokens
                total_output_tokens += llm_res.output_tokens
                total_latency_ms += llm_res.latency_ms

                # Save raw response
                resp_file = attempts_dir / f"attempt_{attempt_idx}_response.md"
                resp_file.write_text(llm_res.text, encoding="utf-8")

                # Parse and merge
                llm_blocks = parse_llm_blocks(llm_res.text)
                merged_draft = merge_llm_blocks_into_skeleton(
                    facts=facts,
                    llm_blocks=llm_blocks,
                    template_path=template_path,
                    provider=provider_name,
                )
                draft_attempt_file = attempts_dir / f"attempt_{attempt_idx}_draft.md"
                draft_attempt_file.write_text(merged_draft, encoding="utf-8")

                # Validate
                report = validator.validate_draft(merged_draft, facts, period)
                findings_file = attempts_dir / f"attempt_{attempt_idx}_findings.json"
                findings_file.write_text(
                    json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                if report.status == "PASS":
                    logger.info("Narrative attempt %d passed validation cleanly.", attempt_idx)
                    final_draft = merged_draft
                    llm_status = "success"
                    break
                else:
                    logger.warning(
                        "Narrative attempt %d failed validation with %d issue(s).",
                        attempt_idx,
                        len(report.issues),
                    )
                    for issue in report.issues:
                        rule_failures[issue.rule_id] = rule_failures.get(issue.rule_id, 0) + 1

                    if attempt_idx < self.max_attempts:
                        repair_prompt = build_repair_prompt(
                            facts=facts,
                            prompts_dir=self.settings.prompts_dir,
                            period=period,
                            previous_response=llm_res.text,
                            report=report,
                            attempt=attempt_idx,
                            max_attempts=self.max_attempts,
                        )
                        repair_file = attempts_dir / f"attempt_{attempt_idx}_repair_prompt.md"
                        repair_file.write_text(repair_prompt, encoding="utf-8")

                        if provider_name == "manual":
                            manual_repair_path = (
                                self.settings.reports_dir / period / "repair_prompt.md"
                            )
                            manual_repair_path.write_text(repair_prompt, encoding="utf-8")
                            manual_resp_path = self.settings.reports_dir / period / "response.md"
                            raise NeedsManualInput(
                                f"Validation failed on attempt {attempt_idx}. Repair prompt "
                                f"written to '{manual_repair_path}'. Update '{manual_resp_path}' "
                                "and re-run.",
                                prompt_path=manual_repair_path,
                                response_path=manual_resp_path,
                            )
                        current_prompt = repair_prompt
                    else:
                        logger.error(
                            "All %d narrative attempts failed validation. "
                            "Falling back to deterministic template draft.",
                            self.max_attempts,
                        )
                        final_draft = deterministic_draft
                        llm_status = "fallback_after_failed_validation"

        # Write reports/<period>/draft.md deterministically
        draft_path = self.settings.reports_dir / period / "draft.md"
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(final_draft, encoding="utf-8")
        logger.info(
            "Wrote draft.md to %s (%d words, status=%s)",
            draft_path,
            len(final_draft.split()),
            llm_status,
        )

        # Write reports/<period>/draft_clean.md for human reading
        clean_markdown = render_clean(final_draft)
        clean_path = self.settings.reports_dir / period / "draft_clean.md"
        clean_path.write_text(clean_markdown, encoding="utf-8")
        logger.info(
            "Wrote clean draft to %s (%d words)",
            clean_path,
            len(clean_markdown.split()),
        )

        # Update reports/<period>/run.json metrics
        run_json_path = self.settings.reports_dir / period / "run.json"
        run_data: dict[str, Any] = {}
        if run_json_path.exists():
            try:
                run_data = json.loads(run_json_path.read_text(encoding="utf-8"))
            except Exception:
                run_data = {}

        model_name = getattr(provider, "model", "deterministic-template")
        llm_metrics = {
            "attempts": recorded_attempts,
            "final_status": llm_status,
            "latency_ms": round(total_latency_ms, 2),
            "llm_mode": provider_name,
            "model": model_name,
            "output_tokens": total_output_tokens,
            "prompt_tokens": total_prompt_tokens,
            "total_tokens": total_prompt_tokens + total_output_tokens,
            "validation_failures_per_rule": dict(sorted(rule_failures.items())),
        }
        run_data["llm"] = llm_metrics
        run_data["llm_mode"] = provider_name
        run_data["llm_status"] = llm_status
        run_json_path.write_text(
            json.dumps(run_data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        narrative_draft = NarrativeDraft(
            period=period,
            markdown=final_draft,
            llm_result=last_llm_result
            or LLMResult(
                text=final_draft,
                provider=provider_name,
                model=model_name,
            ),
            attempt=max(1, recorded_attempts),
        )
        context.draft = narrative_draft
        context.artifacts["narrative_draft"] = narrative_draft

        return StageResult(
            status=StageStatus.SUCCEEDED,
            message=(
                f"Generated research note draft ({len(final_draft.split())} words, "
                f"status={llm_status})"
            ),
            metadata={
                "attempts": recorded_attempts,
                "clean_path": str(clean_path),
                "draft_path": str(draft_path),
                "llm_status": llm_status,
                "provider": provider_name,
            },
        )
