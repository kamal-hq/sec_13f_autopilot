"""Offline security and structural unit tests for GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SECRET_REF_RE = re.compile(r"\$\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}")


def _load_workflow(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"Workflow {path} did not parse to a dict"
    return raw


def _get_on_block(wf: dict[str, Any]) -> dict[str, Any]:
    # PyYAML 1.1 may parse bare `on:` key as boolean True if unquoted
    on_block = wf.get("on") if "on" in wf else wf.get(True)
    assert isinstance(on_block, dict), f"Expected mapping for `on:` trigger block, got {on_block!r}"
    return on_block


def test_autopilot_workflow_dispatch_only_and_inputs() -> None:
    """autopilot.yml triggers via workflow_dispatch ONLY with required inputs and no PR triggers."""
    wf_path = Path(".github/workflows/autopilot.yml")
    raw_text = wf_path.read_text(encoding="utf-8")
    wf = _load_workflow(wf_path)
    on_block = _get_on_block(wf)

    assert list(on_block.keys()) == ["workflow_dispatch"], (
        f"autopilot.yml must trigger on workflow_dispatch ONLY, got {list(on_block.keys())}"
    )
    assert "pull_request" not in on_block
    assert "pull_request_target" not in on_block
    assert "schedule" not in on_block
    assert "#   schedule:" in raw_text, "Commented cron note should be retained in autopilot.yml"

    inputs = (on_block["workflow_dispatch"] or {}).get("inputs") or {}
    assert {"period", "llm", "as_of", "replay"}.issubset(set(inputs.keys()))
    assert inputs["llm"]["options"] == ["none", "gemini"]
    assert inputs["replay"]["type"] == "boolean"


def test_workflows_minimal_permissions_concurrency_and_timeout() -> None:
    """Top-level permissions are contents:read; only autopilot-pr job gets write permissions."""
    for name in ("autopilot.yml", "ci.yml"):
        wf = _load_workflow(Path(".github/workflows") / name)
        assert wf.get("permissions") == {"contents": "read"}, (
            f"{name} top-level permissions must be strictly {{'contents': 'read'}}"
        )
        on_block = _get_on_block(wf)
        assert "pull_request" not in on_block
        assert "pull_request_target" not in on_block

    auto_wf = _load_workflow(Path(".github/workflows/autopilot.yml"))
    assert "concurrency" in auto_wf and "group" in auto_wf["concurrency"]

    jobs = auto_wf.get("jobs") or {}
    assert "autopilot-pr" in jobs
    pr_job = jobs["autopilot-pr"]
    assert pr_job.get("permissions") == {
        "contents": "write",
        "pull-requests": "write",
    }
    assert pr_job.get("timeout-minutes") == 45


def test_every_uses_pinned_to_40_hex_sha_and_no_third_party_pr_action() -> None:
    """Every `uses:` in all workflows is pinned to a 40-hex commit SHA; PR uses gh CLI."""
    workflows_dir = Path(".github/workflows")
    uses_count = 0
    for wf_path in sorted(workflows_dir.glob("*.yml")):
        wf = _load_workflow(wf_path)
        for job_name, job_cfg in (wf.get("jobs") or {}).items():
            for step in (job_cfg or {}).get("steps") or []:
                uses = step.get("uses")
                if not uses:
                    continue
                uses_count += 1
                assert "@" in uses, f"{wf_path.name}:{job_name} unpinned uses: {uses}"
                action_repo, sha = uses.split("@", 1)
                assert _SHA40_RE.match(sha), (
                    f"{wf_path.name}:{job_name} action {action_repo} not 40-hex SHA: {sha}"
                )
                assert action_repo.startswith("actions/"), (
                    f"Only official actions/* allowed (no third-party PR action), got {action_repo}"
                )
    assert uses_count >= 6


def test_secrets_only_in_single_step_env_never_job_or_run() -> None:
    """SEC_USER_AGENT, GEMINI_API_KEY, OPENFIGI_API_KEY are in step-level env of one step only."""
    wf_path = Path(".github/workflows/autopilot.yml")
    wf = _load_workflow(wf_path)
    allowed_secrets = {"SEC_USER_AGENT", "GEMINI_API_KEY", "OPENFIGI_API_KEY"}

    # Never at workflow top-level env
    top_env_str = str(wf.get("env") or "")
    assert not _SECRET_REF_RE.search(top_env_str), "Secrets must never be in workflow-level env"

    secret_steps: list[str] = []
    for job_name, job_cfg in (wf.get("jobs") or {}).items():
        # Never at job-level env
        job_env_str = str((job_cfg or {}).get("env") or "")
        assert not _SECRET_REF_RE.search(job_env_str), (
            f"Secrets must never be in job-level env ({job_name})"
        )

        for step in (job_cfg or {}).get("steps") or []:
            step_name = str(step.get("name", "unnamed"))
            run_script = str(step.get("run") or "")
            # Secrets must never be interpolated into `run:` scripts or echoed
            assert not _SECRET_REF_RE.search(run_script), (
                f"Secrets must never be interpolated directly in `run:` ({step_name})"
            )
            step_env = step.get("env") or {}
            step_env_str = str(step_env)
            if _SECRET_REF_RE.search(step_env_str):
                secret_steps.append(step_name)
                found_secrets = {
                    m.group(0).split("secrets.")[1].rstrip(" }")
                    for m in _SECRET_REF_RE.finditer(step_env_str)
                }
                assert found_secrets == allowed_secrets, (
                    f"Expected allowed secrets {allowed_secrets}, found {found_secrets}"
                )
                assert "detect --emit-period" in run_script
                assert "no actionable period" in run_script
                assert "target_period" not in run_script

    assert len(secret_steps) == 1, (
        f"Expected secrets in env of exactly 1 step, found {len(secret_steps)}: {secret_steps}"
    )
