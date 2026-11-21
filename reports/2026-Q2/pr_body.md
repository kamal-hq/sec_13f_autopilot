# Quarterly 13F Research Note Review Gate — 2026-Q2 `[REPLAY]`

## 1. Quarter-over-Quarter Diff Summary (`diff.md`)

### 1. Headline Portfolio & Signal Metrics
- **Equity-only AUM (excludes options; includes ETFs)**: `$803,285,214,028` (2026-Q1) -> `$917,153,241,621` (2026-Q2) (`+113,868,027,593` / `+14.18%`)
- **Total 13F AUM (Incl. Options)**: `$1,284,805,723,954` (2026-Q1) -> `$1,624,015,283,814` (2026-Q2) (`+339,209,559,860` / `+26.40%`)
- **Broad Convergence Signals (4+ Funds)**: `56` (`31` hedge-fund-only, `25` including discretionary)
- **Discretionary Convergence Signals (2+ of 3 Discretionary Funds)**: `9`
- **New-to-Sample Signals (3+ Funds, No Prior Holders)**: `4`
### 2. Convergence Signal Diff vs Prior Quarter
- **`diff.unavailable`**: Prior-period convergence signals for 2026-Q1 cannot be computed because earlier lookback period 2025-Q4 is not available in database (loaded periods: 2026-Q1, 2026-Q2).
### 3. Top Position Movers (Equity-only AUM)
### Top 5 Operating Company Position Movers
| Rank | Ticker | Company | 2026-Q1 Value | 2026-Q2 Value | QoQ Change |
| :--- | :--- | :--- | ---: | ---: | ---: |
| 1 | `GOOGL/GOOG` | ALPHABET INC | $28,829,065,737 | $53,279,143,729 | +$24,450,077,992 |
| 2 | `AAPL` | APPLE INC | $71,300,271,174 | $78,557,728,582 | +$7,257,457,408 |
| 3 | `AXP` | AMERICAN EXPRESS CO | $46,546,589,731 | $51,771,457,190 | +$5,224,867,459 |
| 4 | `OXY` | OCCIDENTAL PETROLEUM CORP | $17,588,586,275 | $13,276,503,308 | -$4,312,082,967 |
| 5 | `CVX` | CHEVRON CORP | $19,158,759,428 | $15,690,083,965 | -$3,468,675,463 |
### Top 5 ETF Position Movers
| Rank | Ticker | Company | 2026-Q1 Value | 2026-Q2 Value | QoQ Change |
| :--- | :--- | :--- | ---: | ---: | ---: |
| 1 | `IVV` | ISHARES CORE S&P 500 ETF | $5,502,406,539 | $26,909,789,481 | +$21,407,382,942 |
| 2 | `DIA` | STATE STR SPDR DOW JONES IND | $1,561,315,757 | $191,776,160 | -$1,369,539,597 |

## 2. Deterministic Validation Audit: **PASS**

- **Overall Status**: **PASS** (1048 checks executed, 0 final defects)

| Rule | Final Status | Final Defects | Retry Defects Caught |
| :--- | :---: | :---: | :---: |
| **R1** (Number Grounding) | PASS | 0 | 0 |
| **R2** (Citation Integrity) | PASS | 0 | 0 |
| **R3** (Tracked Entities) | PASS | 0 | 0 |
| **R4** (Entity Binding) | PASS | 0 | 0 |
| **R5** (Banned Predictions/Advice) | PASS | 0 | 0 |
| **R6** (Causal Claims) | PASS | 0 | 0 |
| **R7** (Required Content & Limits) | PASS | 0 | 0 |
| **R8** (Stale Superseded Data) | PASS | 0 | 0 |
| **R9** (Formatting & Length Cap) | PASS | 0 | 0 |

## 3. Execution & LLM Metrics

| Metric | Value |
| :--- | :--- |
| **Period** | `2026-Q2` |
| **Execution Mode** | `replay` |
| **LLM Mode** | `none` |
| **Model** | `deterministic-template` |
| **Narrative Attempts** | `0` |
| **Final Narrative Status** | `deterministic` |

### Stage Timings

| Stage | Duration (s) |
| :--- | ---: |
| `detect` | `6.477s` |
| `ingest` | `8.644s` |
| `facts` | `2.790s` |
| `narrate` | `0.002s` |
| `validate` | `0.006s` |
| `review` | `0.000s` |
| **Total** | **`17.919s`** |

## 4. Mandatory Human Reviewer Checklist

- [ ] **Spot-check 3 cited figures against the filings XML or cover pages** (verify 3 cited numbers in `reports/2026-Q2/final_cited.md` against raw SEC Form 13F)
- [ ] **Check issuer names against nameOfIssuer** (verify deterministic `nameOfIssuer` display names and share-class merges such as `GOOGL/GOOG`)
- [ ] **Confirm all required limitations are present** (verify all 6 required `limit.*` disclosures appear in `reports/2026-Q2/final.md`)
- [ ] **Confirm no predictive language or ungrounded causal claims** (verify absence of forward-looking return predictions, trade advice, or causal claims)

## 5. Published Artifacts

- Clean Research Note: [`reports/2026-Q2/final.md`](reports/2026-Q2/final.md)
- Cited Research Note: [`reports/2026-Q2/final_cited.md`](reports/2026-Q2/final_cited.md)
- Validation Report: [`reports/2026-Q2/validation_report.md`](reports/2026-Q2/validation_report.md)
- QoQ Diff Summary: [`reports/2026-Q2/diff.md`](reports/2026-Q2/diff.md)
- Facts Bundle: [`reports/2026-Q2/facts.json`](reports/2026-Q2/facts.json)
- Run Metadata: [`reports/2026-Q2/run.json`](reports/2026-Q2/run.json)
- Broad Signals CSV: [`reports/2026-Q2/exports/signals.csv`](reports/2026-Q2/exports/signals.csv)
- Discretionary Signals CSV: [`reports/2026-Q2/exports/discretionary.csv`](reports/2026-Q2/exports/discretionary.csv)
- New-to-Sample CSV: [`reports/2026-Q2/exports/new_to_sample.csv`](reports/2026-Q2/exports/new_to_sample.csv)
- Fund Summary CSV: [`reports/2026-Q2/exports/fund_summary.csv`](reports/2026-Q2/exports/fund_summary.csv)
