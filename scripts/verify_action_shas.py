"""Verify pinned 40-character GitHub Action SHAs via the unauthenticated public GitHub API.

Usage:
    python scripts/verify_action_shas.py

Reads `.github/workflows/*.yml`, extracts every `uses: owner/repo@sha` reference, asserts
that `sha` is a 40-hex commit hash, and queries `https://api.github.com/repos/<owner>/<repo>/commits/<sha>`
without any credentials or environment variables.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import requests
import yaml

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def collect_workflow_actions(workflows_dir: Path) -> list[tuple[str, str, str]]:
    """Return list of (workflow_filename, repo, ref) from all workflow YAML files."""
    found: list[tuple[str, str, str]] = []
    for wf_path in sorted(workflows_dir.glob("*.yml")):
        data = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
        jobs = data.get("jobs") or {}
        for job_cfg in jobs.values():
            for step in (job_cfg or {}).get("steps") or []:
                uses_val = step.get("uses")
                if not uses_val:
                    continue
                if "@" not in str(uses_val):
                    found.append((wf_path.name, str(uses_val), ""))
                    continue
                repo_part, ref_part = str(uses_val).split("@", 1)
                # Strip any sub-path in action (e.g. owner/repo/subpath@sha -> owner/repo)
                parts = repo_part.strip().split("/")
                owner_repo = "/".join(parts[:2]) if len(parts) >= 2 else repo_part.strip()
                found.append((wf_path.name, owner_repo, ref_part.strip()))
    return found


def verify_sha_on_github(owner_repo: str, sha: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Query public unauthenticated GitHub REST API to verify commit SHA exists."""
    if not _SHA40_RE.match(sha):
        return False, f"Invalid non-40-hex ref: {sha!r}"

    url = f"https://api.github.com/repos/{owner_repo}/commits/{sha}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sec-13f-autopilot-sha-verifier/1.0",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"HTTP request error: {type(exc).__name__}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    payload: dict[str, Any] = resp.json()
    returned_sha = str(payload.get("sha", ""))
    commit_info = payload.get("commit") or {}
    author_date = ((commit_info.get("committer") or {}).get("date")) or "unknown-date"
    first_line = str(commit_info.get("message", "")).splitlines()[0][:45]
    if returned_sha.lower() != sha.lower():
        return False, f"SHA mismatch: got {returned_sha}"
    return True, f"HTTP 200 ({author_date}: {first_line})"


def main(argv: list[str] | None = None) -> int:
    """Verify all workflow action SHAs and print a verification report table."""
    parser = argparse.ArgumentParser(description="Verify GitHub Action 40-hex commit SHAs.")
    parser.add_argument(
        "--workflows-dir",
        type=Path,
        default=Path(".github/workflows"),
        help="Path to GitHub workflows directory.",
    )
    args = parser.parse_args(argv)

    entries = collect_workflow_actions(args.workflows_dir)
    if not entries:
        print("No workflow action `uses:` entries found.")
        return 1

    cache: dict[tuple[str, str], tuple[bool, str]] = {}
    all_ok = True

    print("| Workflow | Action Repository | Pinned 40-Hex SHA | Verification Detail | Status |")
    print("| :--- | :--- | :--- | :--- | :---: |")

    for wf_name, owner_repo, sha in entries:
        key = (owner_repo, sha)
        if key not in cache:
            cache[key] = verify_sha_on_github(owner_repo, sha)
        ok, detail = cache[key]
        status_str = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"| `{wf_name}` | `{owner_repo}` | `{sha}` | {detail} | **{status_str}** |")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
