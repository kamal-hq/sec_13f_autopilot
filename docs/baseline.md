# Manual 13F Workflow Baseline Timing Template

Fill in the fields below after timing a manual end-to-end quarterly 13F update across the 7 tracked institutional funds. Until all three fields (`T1`, `T2`, `T3`) are populated with numeric values, `autopilot learn` and `autopilot metrics --markdown` report `not measured yet` for Manual Baseline and Time Saved.

## Timing Components

- **T1_minutes_per_fund**:
  *(Minutes per fund to locate the latest 13F-HR / 13F-HR/A on SEC EDGAR, download the primary cover page and XML information table, and check for restatements)*
- **T2_minutes_per_fund**:
  *(Minutes per fund to parse XML rows, map CUSIPs/tickers, check stock splits, and reconcile `SUM(value_usd)` against the cover-page `tableValueTotal`)*
- **T3_minutes**:
  *(Minutes across the full cohort to compute Smart Money Convergence signals, quarter-over-quarter position movers, draft the research note, and spot-check figures)*

## Total Manual Baseline Formula

$$\text{Manual Baseline (minutes)} = N_{\text{funds}} \times (T_1 + T_2) + T_3$$

For the default 7-fund cohort ($N_{\text{funds}} = 7$):
- **Total Manual Baseline (minutes)**: not measured yet
