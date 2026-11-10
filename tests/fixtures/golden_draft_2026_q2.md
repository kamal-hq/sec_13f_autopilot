# Quarterly 13F Research Note (2026-Q2)

## Executive Summary

- **Reporting Period**: 2026-Q2 (comparison baseline: 2026-Q1)
- **Equity-only AUM (excludes options; includes ETFs)**: `$270,000,000 [market.aum_equity]` (prior: `$179,000,000 [market.aum_equity_prev]`, change: `$91,000,000 [market.aum_equity_change]` / `+50.84% [market.aum_equity_change_pct]`)
- **Total 13F AUM (Incl. Options)**: `$278,000,000 [market.aum_with_options]` (prior: `$184,000,000 [market.aum_with_options_prev]`, change: `$94,000,000 [market.aum_with_options_change]` / `+51.09% [market.aum_with_options_change_pct]`)
- **Options Derivative AUM**: `$8,000,000 [market.options_aum]` (2.88% [market.options_pct_of_total] of gross portfolio assets)
- **Broad Convergence Signals (4+ funds buying)**: `2 [market.signals_total]` (`1 [market.signals_hedge_only]` hedge-fund-only, `1 [market.signals_with_discretionary]` including discretionary allocators)
- **Discretionary Convergence Signals (2+ of 3 discretionary funds)**: `1 [market.signals_discretionary]`
- **New-to-Sample Signals (3+ funds buying, zero prior holders)**: `1 [market.signals_new_to_sample]`

## Data and Coverage

- **Tracked Institutions Filed**: `6 [coverage.funds_filed]` institutions
- **Cover-Page Value Reconciliation**: `12 [coverage.reconciliations_passed]` of `12 [coverage.reconciliations_total]` filings passed (all reconciled [coverage.all_reconciled])
- **Form 13F-HR/A Restatements Applied**: `1 [coverage.amendments_applied]`

## Broad Convergence

- **`NVDA`** (NVIDIA CORP — Technology): `4 [signal.NVDA.funds_buying]` funds buying, `$53,333,333 [signal.NVDA.dollars_added]` estimated dollars added (Hedge Fund: `4 [signal.NVDA.hedge_fund_buyers]`, Discretionary: `0 [signal.NVDA.discretionary_buyers]`)
- **`AMD`** (ADVANCED MICRO DEVICES INC — Technology): `4 [signal.AMD.funds_buying]` funds buying, `$20,000,000 [signal.AMD.dollars_added]` estimated dollars added (Hedge Fund: `2 [signal.AMD.hedge_fund_buyers]`, Discretionary: `2 [signal.AMD.discretionary_buyers]`)

## Discretionary Convergence

- **`AMD`** (ADVANCED MICRO DEVICES INC — Technology): `2 [discretionary.AMD.funds_buying]` discretionary funds buying, `$13,000,000 [discretionary.AMD.dollars_added]` estimated dollars added [Berkshire Hathaway `$8,000,000 [discretionary.AMD.BERKSHIRE_HATHAWAY.dollars_added]`, Temasek Holdings `$5,000,000 [discretionary.AMD.TEMASEK_HOLDINGS.dollars_added]`]

## New to the Sample

- **`CRWD`** (CROWDSTRIKE HOLDINGS INC — Technology): `3 [new_to_sample.CRWD.funds_buying]` funds initiating, `$6,000,000 [new_to_sample.CRWD.dollars_added]` estimated dollars added

## What Changed Since Last Quarter

- **Persisted Broad Signals**: `1 [diff.signals_persisted]` (NVDA)
- **New Broad Signals**: `1 [diff.signals_new]` (AMD)
- **Dropped Broad Signals**: `1 [diff.signals_dropped]` (MSFT)

### Operating Company Position Movers
- **`NVDA`** (NVIDIA CORP): `$60,000,000 [diff.mover.NVDA.value_change]` [diff.mover.NVDA.is_etf]
- **`AMD`** (ADVANCED MICRO DEVICES INC): `$20,000,000 [diff.mover.AMD.value_change]` [diff.mover.AMD.is_etf]
- **`CRWD`** (CROWDSTRIKE HOLDINGS INC): `$6,000,000 [diff.mover.CRWD.value_change]` [diff.mover.CRWD.is_etf]
- **`AXP`** (AMERICAN EXPRESS CO): `$5,000,000 [diff.mover.AXP.value_change]` [diff.mover.AXP.is_etf]

## Sector View

- **Technology**: `$237,000,000 [sector.Technology.value_usd]` (prior: `$151,000,000 [sector.Technology.value_prev_usd]`, change: `$86,000,000 [sector.Technology.value_change]`, `+56.95% [sector.Technology.value_change_pct]`), `6 [sector.Technology.fund_count]` funds
- **Financials**: `$33,000,000 [sector.Financials.value_usd]` (prior: `$28,000,000 [sector.Financials.value_prev_usd]`, change: `$5,000,000 [sector.Financials.value_change]`, `+17.86% [sector.Financials.value_change_pct]`), `2 [sector.Financials.fund_count]` funds

## Data Quality

- **Confirmed Stock Split**: `KLAC` (2026-06-11) applied `10 [quality.split.KLAC.confirmed]`-for-1 multiplier
- **Unconfirmed Split Candidate**: `CURX` (skipped) flagged `2 [quality.split.CURX.unconfirmed]`-for-1 candidate
- **Restatement Applied**: Citadel Advisors restated filing; total 13F AUM restated to `$67,000,000 [fund.CITADEL_ADVISORS.aum_with_options]`

## Limitations

- **Long-Only Scope**: Form 13F filings reflect exclusively long US equity positions and exclude short positions, cash holdings, foreign exchange, commodities, and unlisted securities [limit.long_only].
- **45-Day Reporting Lag**: Filings report quarter-end positions and are submitted up to 45 calendar days after period close, meaning observed positions may have been modified prior to publication [limit.filing_lag_45d].
- **No Forward Return Test**: Smart Money Convergence identifies historical co-buying patterns across institutional allocators; it does not constitute a forward return backtest or guarantee of future outperformance [limit.no_forward_return_test].
- **Hedge-Only Signals May Reflect Hedging**: Signals driven exclusively by hedge funds may represent arbitrage legs, statistical arbitrage baskets, or delta hedges against short positions rather than unhedged directional conviction [limit.hedge_only_may_be_hedging].
- **Small Discretionary Sample**: The discretionary cohort comprises 3 institutional allocators (Berkshire Hathaway, CalPERS, Temasek Holdings); consensus across 2 funds represents a small sample size that warrants qualitative verification [limit.small_discretionary_sample].
- **Estimated Dollars Added**: Dollars added figures are estimated as shares added multiplied by quarter-end closing price; actual purchase prices vary across the quarter [limit.dollars_added_estimate].

---
*Generated deterministically from filings data; not investment advice.*
