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
- Loads both `target_period` (e.g., `2026-Q2`) and `previous_period` (e.g., `2026-Q1`) into a fresh SQLite star-schema database at `data/autopilot.db` using `sec_13f_analyzer` (`EdgarParser`, `CUSIPMapper`, `SplitDetector`, `DatabaseLoader`) with explicit paths under gitignored `data/`—never calling the library's global `configure()`.
- Reconciles each fund's aggregated SQLite holdings value (`SUM(value_thousands)` in `Fact_Holdings`) and raw information-table row count against the SEC cover-page `tableValueTotal` and `tableEntryTotal`.
- Records confirmed vs. unconfirmed stock splits and `13F-HR/A` restatements applied, persisting `reports/<period>/ingest.json`.

---

## Stage Contracts & `RunLog`

`Pipeline.run(context)` executes stages in strict sequence and populates a `RunLog`:
- Each stage execution produces a `StageLogEntry` with `stage_name`, `status` (`PENDING`, `RUNNING`, `SUCCEEDED`, `SKIPPED`, `AWAITING_REVIEW`, `FAILED`), `started_at`, `finished_at`, `duration_ms`, `attempts`, and `error`.
- When `ReviewStage` returns `StageStatus.AWAITING_REVIEW`, the pipeline halts cleanly and marks `RunLog.status = StageStatus.AWAITING_REVIEW` so `PublishStage` cannot run until human sign-off.
