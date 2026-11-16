"""Feedback loop (`autopilot learn`) and metrics reporting (`autopilot metrics`).

Invariants:
- Never stores secrets, API keys, or prompt/response bodies in `metrics/history.jsonl`.
- Copies approved `final_cited.md` to `style_examples/<period>_final_cited.md` so the prompt
  builder may optionally include at most one style example.
- Renders the Markdown results table with `"not measured yet"` for any unmeasured field and
  computes time-saved percentage ONLY when both `review_minutes` and `manual_baseline_minutes`
  exist.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autopilot.settings import Settings, redact_text

logger = logging.getLogger(__name__)

_REPLAY_BANNER_LINE_RE = re.compile(
    r"^> \*\*Mode\*\*: `replay` \(historical / non-state-mutating execution\)\s*$",
    re.MULTILINE,
)


def _utc_now_iso() -> str:
    """Return current UTC timestamp formatted as ISO-8601."""
    return datetime.now(UTC).isoformat()


def _normalize_for_edit_ratio(markdown: str) -> str:
    """Normalize line endings and strip optional replay banner before edit-ratio diffing."""
    cleaned = _REPLAY_BANNER_LINE_RE.sub("", markdown)
    lines = [line.replace(" [REPLAY]", "").rstrip() for line in cleaned.splitlines()]
    return "\n".join(lines).strip()


def compute_edit_ratio(bot_draft: str, merged_note: str) -> float:
    """Compute human edit ratio in [0.0, 1.0] between bot draft and merged final_cited.md."""
    norm_bot = _normalize_for_edit_ratio(bot_draft)
    norm_merged = _normalize_for_edit_ratio(merged_note)
    if norm_bot == norm_merged:
        return 0.0
    if not norm_bot and not norm_merged:
        return 0.0
    similarity = difflib.SequenceMatcher(None, norm_bot, norm_merged).ratio()
    return round(max(0.0, min(1.0, 1.0 - similarity)), 4)


def parse_baseline_md(
    baseline_path: Path | str = Path("docs/baseline.md"),
    *,
    num_funds: int = 7,
) -> float | None:
    """Parse manual baseline minutes from `docs/baseline.md` if all T1, T2, T3 fields exist."""
    path = Path(baseline_path)
    if not path.exists():
        return None

    text = path.read_text(encoding="utf-8")
    values: dict[str, float] = {}
    for key in ("T1", "T2", "T3"):
        match = re.search(
            rf"(?im)^\s*[-*]?\s*\*?\*?{key}(?:_[a-z0-9_]+)?\*?\*?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\b",
            text,
        )
        if match:
            try:
                values[key] = float(match.group(1))
            except ValueError:
                return None
        else:
            return None

    if len(values) < 3:
        return None
    total = float(num_funds) * (values["T1"] + values["T2"]) + values["T3"]
    return round(total, 2)


def load_one_style_example(
    style_examples_dir: Path | str = Path("style_examples"),
    *,
    max_chars: int = 600,
) -> str | None:
    """Load at most one approved style example excerpt from `style_examples/`."""
    sdir = Path(style_examples_dir)
    if not sdir.exists():
        return None
    candidates = sorted(sdir.glob("*.md"))
    if not candidates:
        return None
    chosen = candidates[-1]
    raw = chosen.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    redacted = redact_text(raw)
    excerpt = redacted[:max_chars].rstrip()
    return excerpt


def learn_from_period(
    settings: Settings,
    period: str,
    *,
    review_minutes: float | None = None,
    baseline_path: Path | str = Path("docs/baseline.md"),
    history_path: Path | str = Path("metrics/history.jsonl"),
    style_examples_dir: Path | str = Path("style_examples"),
) -> dict[str, Any]:
    """Compare merged `final_cited.md` with `draft.md`, append history, and copy style example."""
    period_dir = settings.reports_dir / period
    draft_path = period_dir / "draft.md"
    final_cited_path = period_dir / "final_cited.md"
    if not final_cited_path.exists():
        final_cited_path = period_dir / "final.md"

    if not draft_path.exists():
        raise FileNotFoundError(f"Bot draft not found at {draft_path}")
    if not final_cited_path.exists():
        raise FileNotFoundError(f"Merged final note not found at {final_cited_path}")

    bot_draft = draft_path.read_text(encoding="utf-8")
    merged_note = final_cited_path.read_text(encoding="utf-8")
    edit_ratio = compute_edit_ratio(bot_draft, merged_note)

    run_json_path = period_dir / "run.json"
    run_data: dict[str, Any] = {}
    if run_json_path.exists():
        try:
            run_data = json.loads(run_json_path.read_text(encoding="utf-8"))
        except Exception:
            run_data = {}

    llm_data = dict(run_data.get("llm") or {})
    mode = str(llm_data.get("llm_mode") or run_data.get("llm_mode") or "none")
    if mode == "null":
        mode = "none"
    model = str(llm_data.get("model") or "deterministic-template")
    attempts = int(llm_data.get("attempts") or 0)
    val_failures = dict(llm_data.get("validation_failures_per_rule") or {})
    stage_seconds = dict(run_data.get("stage_seconds") or {})
    total_stage_seconds = round(sum(float(v) for v in stage_seconds.values()), 3)

    manual_baseline = parse_baseline_md(baseline_path, num_funds=settings.min_funds)

    record: dict[str, Any] = {
        "attempts": attempts,
        "edit_ratio": edit_ratio,
        "manual_baseline_minutes": manual_baseline,
        "mode": mode,
        "model": model,
        "period": period,
        "recorded_at": _utc_now_iso(),
        "review_minutes": round(float(review_minutes), 2) if review_minutes is not None else None,
        "stage_seconds": stage_seconds,
        "total_stage_seconds": total_stage_seconds,
        "validation_failures_by_rule": dict(sorted(val_failures.items())),
    }

    hist_file = Path(history_path)
    hist_file.parent.mkdir(parents=True, exist_ok=True)
    serialized = redact_text(json.dumps(record, sort_keys=True))
    with hist_file.open("a", encoding="utf-8") as fh:
        fh.write(serialized + "\n")

    sdir = Path(style_examples_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    style_dest = sdir / f"{period}_final_cited.md"
    shutil.copyfile(final_cited_path, style_dest)

    logger.info(
        "Recorded learn metrics for %s: edit_ratio=%.4f history=%s style_example=%s",
        period,
        edit_ratio,
        hist_file,
        style_dest,
    )
    return record


def format_metrics_markdown(
    history_path: Path | str = Path("metrics/history.jsonl"),
    baseline_path: Path | str = Path("docs/baseline.md"),
) -> str:
    """Render Markdown results table from `metrics/history.jsonl` for the README."""
    hist_file = Path(history_path)
    records: list[dict[str, Any]] = []
    if hist_file.exists():
        for raw_line in hist_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue

    fallback_baseline = parse_baseline_md(baseline_path)

    headers = [
        "| Period | Mode | Model | Attempts | Validation Failures | Stage Seconds | "
        "Edit Ratio | Review Minutes | Manual Baseline | Time Saved |",
        "| :--- | :--- | :--- | :---: | :--- | ---: | ---: | ---: | ---: | ---: |",
    ]

    if not records:
        base_str = (
            f"{fallback_baseline:.1f} min" if fallback_baseline is not None else "not measured yet"
        )
        headers.append(
            "| not measured yet | not measured yet | not measured yet | not measured yet | "
            "not measured yet | not measured yet | not measured yet | not measured yet | "
            f"{base_str} | not measured yet |"
        )
        return "\n".join(headers)

    rows: list[str] = list(headers)
    for rec in records:
        period = str(rec.get("period") or "not measured yet")
        mode = str(rec.get("mode") or "not measured yet")
        model = str(rec.get("model") or "not measured yet")
        attempts = (
            str(rec.get("attempts")) if rec.get("attempts") is not None else "not measured yet"
        )

        val_map = rec.get("validation_failures_by_rule")
        if isinstance(val_map, dict):
            non_zero = [f"{k}:{v}" for k, v in sorted(val_map.items()) if v]
            val_str = ", ".join(non_zero) if non_zero else "0"
        else:
            val_str = "not measured yet"

        total_sec = rec.get("total_stage_seconds")
        stage_sec_map = rec.get("stage_seconds")
        if total_sec is None and isinstance(stage_sec_map, dict) and stage_sec_map:
            total_sec = sum(float(v) for v in stage_sec_map.values())
        sec_str = f"{float(total_sec):.2f}s" if total_sec is not None else "not measured yet"

        edit_val = rec.get("edit_ratio")
        edit_str = f"{float(edit_val):.4f}" if edit_val is not None else "not measured yet"

        rev_min = rec.get("review_minutes")
        rev_str = f"{float(rev_min):.1f} min" if rev_min is not None else "not measured yet"

        man_min = rec.get("manual_baseline_minutes")
        if man_min is None:
            man_min = fallback_baseline
        man_str = f"{float(man_min):.1f} min" if man_min is not None else "not measured yet"

        # Strict rule: No time-saved percentage anywhere unless BOTH numbers exist!
        if rev_min is not None and man_min is not None and float(man_min) > 0:
            saved_pct = (float(man_min) - float(rev_min)) / float(man_min) * 100.0
            saved_str = f"{saved_pct:.1f}%"
        else:
            saved_str = "not measured yet"

        rows.append(
            f"| {period} | {mode} | {model} | {attempts} | {val_str} | "
            f"{sec_str} | {edit_str} | {rev_str} | {man_str} | {saved_str} |"
        )

    return "\n".join(rows)
