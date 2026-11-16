"""Verify pinned 40-hex GitHub Action SHAs and `# vX.Y.Z` tag comments via public GitHub API.

Usage:
    python scripts/verify_action_shas.py

Reads `.github/workflows/*.yml`, extracts every `uses: owner/repo@sha # vX.Y.Z` line, asserts
that `sha` is a 40-hex commit hash, verifies the commit exists via the unauthenticated public
GitHub REST API, and resolves the `# vX.Y.Z` tag (dereferencing annotated tags) to confirm the
version tag points to the exact pinned commit SHA.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_USES_LINE_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*([^\s@#]+)@([^\s#]+)(?:\s*#\s*(v[0-9A-Za-z._\-]+))?\s*$"
)
_HEADERS: dict[str, str] = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "sec-13f-autopilot-sha-verifier/1.0",
}
_SESSION = requests.Session()
_SESSION.headers.update(_HEADERS)


def _get_json(
    url: str, timeout: float = 15.0, retries: int = 5
) -> tuple[int, dict[str, Any] | str]:
    """Perform unauthenticated GET with shared keep-alive session and retry on resets."""
    last_err = ""
    for attempt in range(retries):
        try:
            resp = _SESSION.get(url, timeout=timeout)
            if resp.status_code == 200:
                return 200, resp.json()
            return resp.status_code, f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}"
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    return 0, f"HTTP request error: {last_err}"


def collect_workflow_actions(workflows_dir: Path) -> list[tuple[str, str, str, str]]:
    """Return list of (workflow_filename, owner_repo, sha, tag_comment) from workflow YAML files."""
    found: list[tuple[str, str, str, str]] = []
    for wf_path in sorted(workflows_dir.glob("*.yml")):
        for raw_line in wf_path.read_text(encoding="utf-8").splitlines():
            match = _USES_LINE_RE.match(raw_line)
            if not match:
                continue
            repo_part, sha_part, tag_part = (
                match.group(1),
                match.group(2),
                match.group(3) or "",
            )
            parts = repo_part.strip().split("/")
            owner_repo = "/".join(parts[:2]) if len(parts) >= 2 else repo_part.strip()
            found.append((wf_path.name, owner_repo, sha_part.strip(), tag_part.strip()))
    return found


def resolve_tag_to_commit_sha(
    owner_repo: str,
    tag: str,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """Resolve `tag` via public GitHub Git refs API, dereferencing annotated tags to commit SHA."""
    if not tag:
        return False, "Missing `# vX.Y.Z` version comment on `uses:` line"

    ref_url = f"https://api.github.com/repos/{owner_repo}/git/ref/tags/{tag}"
    code, payload = _get_json(ref_url, timeout=timeout)
    if code != 200 or not isinstance(payload, dict):
        return False, f"Tag {tag!r} lookup {payload}"

    obj: dict[str, Any] = payload.get("object") or {}
    obj_type = str(obj.get("type", ""))
    obj_sha = str(obj.get("sha", ""))

    # Dereference annotated tag objects until we reach the underlying commit
    hops = 0
    while obj_type == "tag" and obj_sha and hops < 3:
        hops += 1
        tag_obj_url = f"https://api.github.com/repos/{owner_repo}/git/tags/{obj_sha}"
        t_code, tag_data = _get_json(tag_obj_url, timeout=timeout)
        if t_code != 200 or not isinstance(tag_data, dict):
            return False, f"Annotated tag dereference {tag_data}"
        inner_obj: dict[str, Any] = tag_data.get("object") or {}
        obj_type = str(inner_obj.get("type", ""))
        obj_sha = str(inner_obj.get("sha", ""))

    if obj_type != "commit" or not obj_sha:
        return False, f"Tag {tag!r} resolved to non-commit ({obj_type}:{obj_sha})"
    return True, obj_sha


def verify_sha_and_tag_on_github(
    owner_repo: str,
    sha: str,
    tag: str,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """Verify commit SHA exists AND `# vX.Y.Z` tag resolves to the exact pinned commit SHA."""
    if not _SHA40_RE.match(sha):
        return False, f"Invalid non-40-hex ref: {sha!r}"

    commit_url = f"https://api.github.com/repos/{owner_repo}/commits/{sha}"
    code, payload = _get_json(commit_url, timeout=timeout)
    if code != 200 or not isinstance(payload, dict):
        return False, f"Commit {payload}"

    returned_sha = str(payload.get("sha", ""))
    if returned_sha.lower() != sha.lower():
        return False, f"Commit SHA mismatch: got {returned_sha}"

    tag_ok, tag_result = resolve_tag_to_commit_sha(owner_repo, tag, timeout=timeout)
    if not tag_ok:
        return False, tag_result
    if tag_result.lower() != sha.lower():
        return False, f"Tag {tag} points to {tag_result}, expected {sha}"

    commit_info = payload.get("commit") or {}
    author_date = ((commit_info.get("committer") or {}).get("date")) or "unknown-date"
    return True, f"Commit HTTP 200 & tag {tag} -> {tag_result[:12]}... ({author_date})"


def main(argv: list[str] | None = None) -> int:
    """Verify all workflow action SHAs and tag comments and print a verification table."""
    parser = argparse.ArgumentParser(
        description="Verify GitHub Action 40-hex commit SHAs and `# vX.Y.Z` version tag comments."
    )
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

    cache: dict[tuple[str, str, str], tuple[bool, str]] = {}
    all_ok = True

    print(
        "| Workflow | Action Repository | Tag | Pinned 40-Hex SHA | "
        "Tag & Commit Verification | Status |"
    )
    print("| :--- | :--- | :--- | :--- | :--- | :---: |")

    for wf_name, owner_repo, sha, tag in entries:
        key = (owner_repo, sha, tag)
        if key not in cache:
            cache[key] = verify_sha_and_tag_on_github(owner_repo, sha, tag)
        ok, detail = cache[key]
        status_str = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"| `{wf_name}` | `{owner_repo}` | `{tag}` | `{sha}` | {detail} | **{status_str}** |")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
