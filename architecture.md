# Architecture

## Design Principles

`sec_13f_autopilot` enforces strict separation between **external I/O gateways**, **deterministic quantitative stages**, and **governed narrative generation**:

1. **Gateway Isolation**: Only classes under `src/autopilot/gateways/` (`HttpClient`, `SecGateway`, `LLMProvider` implementations) interact with the network or external file-exchange prompts.
2. **Single Source of Quantitative Truth**: `sec_13f_analyzer` owns the SQLite star schema (`Dim_Fund`, `Dim_Security`, `Fact_Holdings`) and Smart Money Convergence SQL queries. `FactsStage` packages those outputs into an immutable `FactsBundle` with an explicit `allowed_numbers` set.
3. **Fail-Closed Validation & Human Sign-Off**: `ValidateStage` rejects any narrative draft containing numbers outside `FactsBundle.allowed_numbers` (retrying `NarrateStage` up to 3 times within `BudgetGuard`). `ReviewStage` halts before `PublishStage` until human approval is recorded.

---

## Module Layout

```text
sec_13f_autopilot/
├── .github/workflows/
│   ├── ci.yml                # Ruff + Pytest on push (SHA-pinned, contents: read)
│   └── autopilot.yml         # workflow_dispatch ONLY (cron commented re: 60-day rule)
├── docs/
│   └── runbook.md            # Operational runbook for manual & one-click runs
├── prompts/
│   ├── system.md             # System prompt placeholder
│   ├── note_template.md      # Research note template placeholder
│   └── style_guide.md        # Institutional style guide placeholder
├── reports/                  # Staged & published per-quarter reports (.gitkeep)
├── state/                    # Ingestion watermarks & RunLog JSON (.gitkeep)
├── src/autopilot/
│   ├── settings.py           # Env-only secrets, masked __repr__, SecretRedactionFilter
│   ├── models.py             # Dataclasses: Filing, Period, DetectResult, IngestResult, RunLog...
│   ├── state.py              # StateStore for state/processed_filings.json (atomic os.replace writes)
│   ├── policies.py           # PeriodCompletenessPolicy, rate-limit constants, RetryPolicy
│   ├── pipeline.py           # Stage ABC, ordered Pipeline runner (supports --through), RunLog
│   ├── cli.py                # CLI entrypoint (`autopilot detect`, `ingest`, `run --through`)
│   ├── gateways/
│   │   ├── http.py           # Shared requests.Session, RateLimiter, exponential backoff
│   │   ├── sec.py            # SecGateway (<=8 req/s, User-Agent from Settings, cover-page inspection)
│   │   └── llm.py            # LLMProvider ABC, Gemini/Manual/Null providers, BudgetGuard
│   └── stages/
│       ├── base.py           # Stage ABC, StageResult, StageError, StageSkip
│       ├── detect.py         # DetectStage (SEC submissions, replay --as-of, completeness & amendments)
│       ├── ingest.py         # IngestStage (2-period load into data/autopilot.db & cover-page reconciliation)
│       ├── facts.py          # FactsStage contract stub
│       ├── narrate.py        # NarrateStage contract stub
│       ├── validate.py       # ValidateStage contract stub
│       ├── review.py         # ReviewStage contract stub
│       └── publish.py        # PublishStage contract stub
└── tests/                    # 100% offline unit tests with recorded fixtures & mocked HTTP/SDK
```

---

## Gateway Specifications

### 1. `gateways/http.py` (`HttpClient` & `RateLimiter`)
- Wraps a shared `requests.Session` with configurable per-request timeouts (`timeout_seconds`).
- Thread-safe `RateLimiter` enforces minimum inter-request spacing (`1.0 / max_calls_per_second`) using injectable `clock_fn` and `sleep_fn` for deterministic testing.
- Retries HTTP `429`, `500`, `502`, `503`, and `504` responses (plus `requests.Timeout` and `requests.RequestException`) with exponential backoff (`base * 2^attempt`, honoring numeric `Retry-After` headers when present).
- Emits typed errors: `HttpRateLimitError`, `HttpServerError`, `HttpClientError`, `HttpTimeoutError`, `HttpConnectionError`.

### 2. `gateways/sec.py` (`SecGateway`)
- Reads `User-Agent` exclusively from `Settings.sec_user_agent` and raises `SecConfigurationError` if missing, malformed, or left as a placeholder.
- Enforces a hard rate limit cap of **<= 8.0 requests/second** (comfortably below SEC EDGAR's 10 req/s ceiling).
- Translates HTTP failures into typed SEC errors (`SecRateLimitError`, `SecServerError`, `SecNotFoundError`, `SecClientError`, `SecNetworkError`).

### 3. `gateways/llm.py` (`LLMProvider` & `BudgetGuard`)
- **`GeminiProvider`**: Uses the official `google-genai` SDK (`from google import genai`), reads the API key solely via `settings.read_gemini_api_key()`, backs off on HTTP `429` / `RESOURCE_EXHAUSTED`, and hardcodes no quotas.
- **`ManualProvider`**: Writes `reports/<period>/prompt.md` and reads `reports/<period>/response.md`, raising `NeedsManualInput` if the response file is absent or empty.
- **`NullProvider`**: Produces a deterministic, offline markdown research note draft.
- **`BudgetGuard`**: Enforces `max_calls_per_run` (default `5`), `max_output_tokens`, `timeout_seconds`, and the `AUTOPILOT_LLM_DISABLED=1` kill switch.

---

## Phase 1: State, Detection, Replay & Ingestion

### 1. `state.py` (`StateStore`)
- Manages `state/processed_filings.json` with `schema_version` and an `accession -> {period, filed_date, processed_at, supersedes}` map.
- Uses atomic writes (`tempfile.NamedTemporaryFile` in the same directory + `os.fsync` + `os.replace`) so interrupted runs never leave corrupt state.
- **Strict Stage Invariant**: `DetectStage` and `IngestStage` read `StateStore` but **never** write to `state/processed_filings.json` (state updates occur exclusively in Phase 5 `PublishStage`).

### 2. `stages/detect.py` (`DetectStage`) & `PeriodCompletenessPolicy`
- Polls SEC EDGAR submissions via `SecGateway` (`get_latest_13f_metadata(..., inspect_cover_pages=True)`), filtering `13F-HR` and `13F-HR/A` filings and respecting `--as-of YYYY-MM-DD` replay cutoffs.
- `PeriodCompletenessPolicy` evaluates each period into `PeriodStatus`:
  - `PENDING`: Zero configured funds have filed.
  - `PARTIAL`: Fewer than all configured funds have filed (actionable only when `allow_partial=True` and `len(filed) >= min_funds`).
  - `COMPLETE`: All configured funds have filed and at least one accession is unprocessed.
  - `PROCESSED`: All active accessions are already recorded in `StateStore` (idempotent no-op).
  - `RESTATED`: Period was previously `PROCESSED`, and a new `13F-HR/A` (`RESTATEMENT`) supersedes a previously processed accession, generating a changelog entry and triggering re-ingestion.
  - `NEEDS_REVIEW`: A `13F-HR/A` (`NEW HOLDINGS`) partial amendment is present; auto-processing is blocked for manual inspection.
- Persists `reports/<period>/detect.json` (unless `--dry-run` is set).

### 3. `stages/ingest.py` (`IngestStage`)
- Loads both `target_period` (e.g., `2026-Q2`) and up to `lookback_periods` prior periods into a fresh SQLite star-schema database at `data/autopilot.db` using `sec_13f_analyzer` (`EdgarParser`, `CUSIPMapper`, `SplitDetector`, `DatabaseLoader`) with explicit paths under gitignored `data/`—never calling the library's global `configure()`.
- Reconciles each fund's aggregated SQLite holdings value (`SUM(value_usd)` in `Fact_Holdings`) and raw information-table row count against the SEC cover-page `tableValueTotal`.
- Records confirmed vs. unconfirmed stock splits and `13F-HR/A` restatements applied, persisting `reports/<period>/ingest.json`.

---

## Phase 2: Quantitative Facts Extraction & Diff Generation (`FactsStage`)

### 1. Pure Deterministic Facts (`stages/facts.py`)
- **Zero Network Calls**: Pure deterministic computation calling `sec_13f_analyzer` with explicit `db_path`.
- **Output Artifacts**:
  - `reports/<period>/facts.json`: Timestamp-free, sorted-key JSON document containing every semantic `FactItem`, allowable numbers list, and raw metrics. Byte-identical across repeated runs.
  - `reports/<period>/diff.md`: Short, human-readable markdown diff of portfolio metrics, sector shifts, signal transitions, and position movers between quarters.
  - `reports/<period>/run.json`: Execution run metadata containing execution timestamp (`generated_at`) and fact counts by kind, preserving timestamp-free byte reproducibility in `facts.json`.
- **Semantic Fact Structure (`FactItem`)**:
  - `id`: Unique, semantic, stable identifier (e.g., `signal.AMD.funds_buying`, `discretionary.GOOGL_GOOG.dollars_added`, `fund.CALPERS.aum_equity`, `market.signals_total`).
  - `kind`: Category (`metadata`, `coverage`, `market`, `signal`, `discretionary`, `new_to_sample`, `sector`, `fund`, `quality`, `limit`, `diff`).
  - `value`: Raw int or float with fixed precision.
  - `display`: Deterministic list of human renderings (e.g., `"$18.67B"`, `"$18,665,002,497"`).
  - `period`: Associated reporting quarter (`YYYY-QN`).
  - `source`: Provenance dictionary detailing the library function and exact parameters.
- **Lookback & Diff Handling**:
  - `lookback_periods` (default `2`): Ingestion retains target quarter plus prior lookback quarters.
  - When the prior comparison quarter is missing from the database, emits a semantic `diff.unavailable` fact explaining the reason, preventing narrative hallucination.
- **Input Sanitization**:
  - Every external fund name and company issuer is sanitized via `sanitize_external_name` (stripping HTML/XML markup, Unicode control codes, prompt injection patterns, collapsing whitespace, max 60 chars).
- **Mandatory Disclaimers & Limits**:
  - Encodes mandatory limitation facts: `limit.long_only`, `limit.filing_lag_45d`, `limit.no_forward_return_test`, `limit.hedge_only_may_be_hedging`, and `limit.small_discretionary_sample`.

---

## Phase 3a: Deterministic Template Narration (`NarrateStage`)

### 1. Research Note Draft Generation (`stages/narrate.py`)
- **Deterministic Draft Generation**: With `NullProvider` (`--llm none`), produces an immutable, offline research note draft `reports/<period>/draft.md` without invoking LLM APIs or touching credentials.
- **Strict Evidence Citation**: Every quantitative figure (dollars, counts, percentages, split factors, multipliers) in the draft is immediately followed by its bracketed fact id citation, e.g. `$18.67B [discretionary.GOOGL_GOOG.dollars_added]`.
- **9 Canonical Note Sections (`prompts/note_template.md`)**:
  1. `Executive Summary`: Portfolio headline AUM, options breakdown, broad and discretionary convergence counts.
  2. `Data and Coverage`: Funds filed, cover-page reconciliation pass counts, restatements and stock splits.
  3. `Broad Convergence`: Top broad convergence signals (4+ funds buying, sorted by dollars added, breakdown by fund type).
  4. `Discretionary Convergence`: Long-horizon allocators (Berkshire Hathaway, CalPERS, Temasek Holdings) co-buying.
  5. `New to the Sample`: Securities initiated by 3+ funds with zero prior holders in the sample.
  6. `What Changed Since Last Quarter`: Signal persistence/new/dropped transitions (or `diff.unavailable` explanation), plus top position movers separated into Operating Companies (merged share classes, e.g., `GOOGL/GOOG`) and ETFs.
  7. `Sector View`: Sector allocations, QoQ shifts, fund counts, and context notes (`sector.Unknown.note`).
  8. `Data Quality`: Confirmed stock split adjustments, unconfirmed candidate flags, and restatement accession tags.
  9. `Limitations`: Mandatory citation of all 5 required limitation facts (`limit.long_only`, `limit.filing_lag_45d`, `limit.no_forward_return_test`, `limit.hedge_only_may_be_hedging`, `limit.small_discretionary_sample`).
- **Mandatory Disclaimer**: Every note concludes with the exact disclaimer:
  `*Not investment advice; AI-drafted, figures checked against filings data.*`

---

## Phase 3b: Deterministic Validator & Quality Fixes (`ValidateStage`)

### 1. Deterministic Audit Rules (`stages/validate.py`)
`ValidateStage` consumes `reports/<period>/draft.md` and `reports/<period>/facts.json` without network access or LLM calls, generating `reports/<period>/validation_report.json` and `.md`:
- **R1 Number Grounding**: Every numeric token (currency, percentage, integer count, split multiplier) must be followed by `[fact_id]` and match the fact value: exactly matching a listed `display` format, or rounded to 2 significant figures within 2% relative tolerance. Units must agree ($M vs $B).
- **R2 Citation Integrity**: Every cited `[fact_id]` must exist in `facts.json`; no orphan or hallucinated citations.
- **R3 Entity Validation**: Tracked institutional funds and ticker symbols in the prose must exist in `facts.json`.
- **R4 Entity Binding**: Cited fund breakdown facts (e.g., `discretionary.GOOGL_GOOG.BERKSHIRE_HATHAWAY.dollars_added`) and position mover facts must bind to the entity named in their clause/bullet.
- **R5 Banned Advice Language**: Predictions, speculative assertions, and financial advice (`will outperform`, `expected to`, `should buy`, `should invest`, `should accumulate`, `accumulate`, `alpha`, `guaranteed`, `undervalued`) are prohibited outside Limitations. Inside Limitations, they are permitted only when negated.
- **R6 Causal Claims**: Verbs of cause (`because`, `due to`, `driven by`, `as a result of`) linking market movements to external events are forbidden outside Limitations.
- **R7 Required Content**: All 9 canonical section headings, 6 required `limit.*` facts (`limit.long_only`, `limit.filing_lag_45d`, `limit.no_forward_return_test`, `limit.hedge_only_may_be_hedging`, `limit.small_discretionary_sample`, `limit.dollars_added_estimate`), and valid reporting period metadata must be present.
- **R8 Stale Data (Superseded Facts)**: Detects and rejects superseded pre-amendment values (e.g., Citadel's pre-amendment total of $875,096,281,441 vs restated $875,011,794,337) using facts flagged with `superseded: true`.
- **R9 Formatting & Length Guardrails**: Rejects URLs (`http://`, `https://`, `www.`), markdown links (`[text](url)`), HTML tags (`<...>`), code fences (`` ``` `` or `~~~`), and narrative blocks exceeding 150 words.

### 2. Adversarial Suite (`tests/adversarial/cases.yaml`)
- Evaluates 20 bad mutations (each systematically rejected by rules R1–R9) and 7 good mutations (including $k$-sig-fig boundary rounding cases for $k \ge 2$) accepted cleanly with status `PASS`.
- Rigorously asserts `bad rejected 20/20, good accepted 7/7`, and fails the test run if any rule R1–R9 has zero exercising cases.

### 3. Display Name Determinism & Common Stock Merging
- Primary display names for securities derive deterministically from the filing `nameOfIssuer` (via `data/mappings/filing_names.json`), while OpenFIGI is used strictly for ticker mapping.
- Operating company position movers merge common stock classes (e.g., `GOOGL/GOOG` equals $24,450,077,992) and exclude non-common equity depositary shares ($476,936,007) and options.
- Clean draft rendering: `render_clean(draft)` generates `reports/<period>/draft_clean.md` stripping citation tags for institutional reading.

---

## Phase 4: Governed LLM Narrative & Validation Retry Loop (`NarrateStage`)

- **Compact Fact Sheet Only**: The LLM (`GeminiProvider` or `ManualProvider`) writes only concise narrative commentary blocks (`Executive Summary` and one short paragraph per major section) while tables, lists, and limitations remain 100% deterministic. The prompt builder (`build_narrative_prompt`) passes only a compact `id | label | display` fact sheet (zero file paths, zero environment variables, zero raw XML).
- **Automated Repair & Fallback Loop**:
  - Generates narrative blocks (up to `--max-attempts 3`), merges into the deterministic skeleton, and validates via `ValidateStage`.
  - On `FAIL`, builds a targeted repair prompt (`build_repair_prompt`) with the exact rule ID, line number, defect snippet, and expected value from `facts.json`.
  - Persists every attempt (`attempt_{k}_prompt.md`, `attempt_{k}_response.md`, `attempt_{k}_draft.md`, `attempt_{k}_findings.json`, `attempt_{k}_repair_prompt.md`) under `reports/<period>/attempts/`.
  - Falls back cleanly to the deterministic template draft after persistent validation failures (`llm_status="fallback_after_failed_validation"`) or HTTP 429 quota exhaustion (`llm_status="quota_exhaustion_fallback"`).

---

## Phase 5: Publish Stage, GitHub Action PR Gate, Feedback Loop & Metrics

### 1. Publish Stage (`stages/publish.py`)
- Verifies that `ValidateStage` passed (`status == "PASS"`), then writes under `reports/<period>/`:
  - `final.md`: Clean human-facing research note with no backticks around figures, validator-accepted short USD display forms (such as `"$917.2B"`), and all `[fact_id]` citations stripped.
  - `final_cited.md`: Cited research note retaining all `[fact_id]` provenance tags.
  - `validation_report.md`, `diff.md`, `run.json` (scrubbed via `redact_text`), and `pr_body.md`.
  - `exports/`: Deterministic CSV exports (`signals.csv`, `discretionary.csv`, `new_to_sample.csv`, `fund_summary.csv`).
- **State Gate (`--commit-state` vs `--replay`)**:
  - Without `--commit-state`, `state/processed_filings.json` is never modified.
  - With `publish --commit-state` (used on the PR branch so merging the PR into `main` is the human approval gate), marks the period's accessions as processed in `state/processed_filings.json`.
  - With `--replay`, labels outputs `[REPLAY]` / `replay` and **never** touches `state/processed_filings.json`.

### 2. Pull Request Body Builder (`build_pr_body`)
- Pure function rendering `reports/<period>/pr_body.md` with:
  1. Quarter-over-quarter `diff.md` summary.
  2. Validation `PASS`/`FAIL` status and per-rule defect table (`R1`–`R9`).
  3. Execution & LLM metrics table (`mode`, `model`, `attempts`, `stage_seconds`).
  4. Mandatory 4-item human reviewer checklist (`3 figures spot-checked against the filings`, `issuer names checked`, `limitations present`, `no predictive language`).
  5. Relative links to all published markdown and CSV artifacts.

### 3. Feedback Loop & Metrics (`metrics.py`)
- `autopilot learn --period X [--review-minutes M]` computes the human edit ratio (`1.0 - SequenceMatcher.ratio()`) between the merged `final_cited.md` and the bot's `draft.md`, appends a secret-free record to `metrics/history.jsonl`, and copies the approved note to `style_examples/` (from which `build_narrative_prompt` may include at most one excerpt).
- `autopilot metrics --markdown` renders the README Results table from `metrics/history.jsonl` and `docs/baseline.md`, outputting `not measured yet` for any empty field and computing `Time Saved (%)` only when both `review_minutes` and `manual_baseline_minutes` exist.

### 4. Phase 5b: Actionable Period Auto-Detection, Seed Caches & State Seeding
- **Period Auto-Selection (`autopilot detect --emit-period`)**: `select_actionable_period(detect_result)` selects the most recent period whose status is `COMPLETE` or `RESTATED` (never `PROCESSED`) and whose immediate baseline period (`compute_previous_period(period)`) is available. If none qualifies, prints `"no actionable period"` and exits `0` without falling back to `config.yaml` `target_period`.
- **Seed Caches (`seed/` & `scripts/refresh_seed.py`)**: Tracked public caches (`cusip_cache.json`, `sector_cache.json`, `split_cache.json`, `filing_metadata.json`) in `seed/` are copied/merged into `data/mappings/` on a cache miss by `ensure_seed_caches()` before `IngestStage` calls `map_cusips`, logging total CUSIPs, cached hits, API hits, and elapsed seconds.
- **State Seeding (`autopilot seed-state --through 2026-Q2 [--yes]`)**: `seed_state_through()` lists all historical accessions `<= through_period` (dry-run by default) and writes `state/processed_filings.json` when `--yes` is passed.
- **Action SHA & Tag Verifier (`scripts/verify_action_shas.py`)**: Verifies every pinned 40-hex action SHA exists on GitHub and dereferences the `# vX.Y.Z` tag comment (including annotated tag objects) via the unauthenticated public GitHub API to confirm the version tag resolves to the pinned commit SHA.

---

## Stage Contracts & `RunLog`

`Pipeline.run(context)` executes stages in strict sequence and populates a `RunLog`:
- Each stage execution produces a `StageLogEntry` with `stage_name`, `status` (`PENDING`, `RUNNING`, `SUCCEEDED`, `SKIPPED`, `AWAITING_REVIEW`, `FAILED`), `started_at`, `finished_at`, `duration_ms`, `attempts`, and `error`.
- When `ReviewStage` returns `StageStatus.AWAITING_REVIEW`, the pipeline halts cleanly and marks `RunLog.status = StageStatus.AWAITING_REVIEW` so `PublishStage` cannot run until human sign-off.
