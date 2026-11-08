# Operational Runbook

## 1. One-Click Manual Run (`workflow_dispatch`)
1. Navigate to **Actions -> Quarterly 13F Autopilot** in GitHub.
2. Click **Run workflow** and select:
   - `target_period` (e.g., `2026-Q2`)
   - `llm_provider` (`null`, `manual`, or `gemini`)
   - `dry_run` (`true` or `false`)
3. Download the staged artifact (`autopilot-<period>-artifacts`), inspect `reports/<period>/` and `state/last_run.json`, and open/approve the review Pull Request before publishing.

## 2. Local Human-in-the-Loop Run (`ManualProvider`)
When running without an API key using `ManualProvider`:
1. Run `autopilot --period 2026-Q2 --provider manual`.
2. On the first run, `ManualProvider` writes `reports/2026-Q2/prompt.md` and raises `NeedsManualInput`.
3. Paste the verified markdown response into `reports/2026-Q2/response.md`.
4. Re-run `autopilot --period 2026-Q2 --provider manual` to proceed through `ValidateStage` and `ReviewStage`.

## 3. Emergency LLM Kill Switch
Set `AUTOPILOT_LLM_DISABLED=1` in the environment to block all guarded LLM calls immediately and fall back to offline deterministic behavior.
