# AGENTS.md — Rules for Future AI Agents

Any AI coding assistant or automated agent modifying `sec_13f_autopilot` **MUST** obey the following non-negotiable engineering and security rules:

## 1. Never Print or Commit Secrets
- **No `print()` statements**: Use Python's `logging` module exclusively (`ruff` rule `T20` enforces this in CI and pre-commit).
- **Secrets from environment only**: `GEMINI_API_KEY`, `SEC_USER_AGENT`, and `OPENFIGI_API_KEY` must only be read from environment variables via `autopilot.settings.Settings`. Never place credentials in `config.yaml`, source files, test fixtures, or commit messages.
- **Preserve secret masking & log redaction**:
  - `Settings.__repr__` and `Settings.__str__` must always mask secrets with `'***REDACTED***'`.
  - Only `GeminiProvider` is permitted to call `settings.read_gemini_api_key()`.
  - `SecretRedactionFilter` must remain attached to logging handlers and redact literal secrets as well as `key=...`, `Authorization`, and `AIza...` patterns.
  - Never log HTTP request bodies, LLM prompts, or LLM response bodies—log only status codes, latency, and token counts.

## 2. Only Gateways Touch the Network
- All external HTTP requests and SDK calls must live inside `src/autopilot/gateways/` (`http.py`, `sec.py`, `llm.py`).
- Pipeline stages (`src/autopilot/stages/`), domain models (`models.py`), policies (`policies.py`), and orchestrators (`pipeline.py`) must never import `requests`, `urllib`, `httpx`, or `google.genai` directly; they must accept gateway interfaces via dependency injection.
- `SecGateway` must never exceed `8.0` requests per second (`MAX_SEC_REQUESTS_PER_SECOND = 8.0`).

## 3. Every Number Must Come From `FactsBundle`
- Quantitative metrics (portfolio values, share counts, QoQ percentage changes, buyer counts, Smart Money Convergence signals) must be computed deterministically by `sec_13f_analyzer` and packaged into `FactsBundle` (`facts.py`).
- Prompts and LLM providers must never invent, extrapolate, or estimate numbers.
- `ValidateStage` must verify every numeric token in `NarrativeDraft` against `FactsBundle.allowed_numbers` (with at most 3 retries bounded by `BudgetGuard`).
- Every generated research note must retain the disclaimer: *"Not investment advice; AI-drafted, figures checked against filings data."*

## 4. Tests Must Run 100% Offline
- Every test in `tests/` must execute offline without network access and without real API keys.
- Mock `requests.Session` / `sleep_fn` / `clock_fn` when testing `HttpClient` and `SecGateway`, and mock the Gemini client when testing `GeminiProvider`.
- Always run `ruff check src tests`, `pytest`, and `pre-commit run --all-files` before committing.
