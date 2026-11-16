"""Command-line entrypoint for `sec_13f_autopilot` (uses logging only, never `print`).

Supported commands:
- `autopilot detect [--as-of YYYY-MM-DD] [--period YYYY-QN] [--dry-run]`
- `autopilot ingest --period 2026-Q2 [--as-of YYYY-MM-DD] [--dry-run]`
- `autopilot facts --period 2026-Q2 [--dry-run]`
- `autopilot run [--through STAGE] [--period YYYY-QN] [--as-of YYYY-MM-DD] [--dry-run]`
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from autopilot.metrics import format_metrics_markdown, learn_from_period
from autopilot.models import NarrativeDraft, PipelineContext, StageStatus
from autopilot.pipeline import Pipeline, build_default_stages
from autopilot.policies import compute_previous_period
from autopilot.settings import Settings, configure_logging
from autopilot.stages.base import StageError, StageResult
from autopilot.stages.detect import DetectStage, select_actionable_period
from autopilot.stages.facts import FactsStage
from autopilot.stages.ingest import IngestStage
from autopilot.stages.narrate import NarrateStage
from autopilot.stages.publish import PublishStage
from autopilot.stages.validate import ValidateStage
from autopilot.state import seed_state_through

logger = logging.getLogger(__name__)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    """Attach shared CLI arguments (`--config`, `--as-of`, `--dry-run`) to a parser."""
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to non-secret YAML configuration file (default: config.yaml).",
    )
    parser.add_argument(
        "--as-of",
        dest="as_of",
        type=str,
        default=None,
        help="Replay cutoff date (YYYY-MM-DD); ignores filings filed after this date.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute without writing reports, databases, or state files.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser with `detect`, `ingest`, `facts`, and `run` subcommands."""
    parser = argparse.ArgumentParser(
        prog="autopilot",
        description="Human-approved, one-click quarterly SEC 13F update pipeline.",
    )
    _add_common_flags(parser)
    parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Override target reporting period (e.g., 2026-Q2).",
    )
    parser.add_argument(
        "--provider",
        "--llm",
        dest="llm_provider",
        type=str,
        choices=["null", "none", "manual", "gemini", "auto"],
        default=None,
        help="Override LLM provider selection (null/none, manual, gemini, auto).",
    )

    subparsers = parser.add_subparsers(dest="command")

    # 1. `autopilot detect [--as-of YYYY-MM-DD] [--emit-period] [--dry-run]`
    detect_parser = subparsers.add_parser(
        "detect",
        help="Detect new 13F-HR and 13F-HR/A filings across configured funds.",
    )
    _add_common_flags(detect_parser)
    detect_parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Target reporting period to focus on (default: from config.yaml).",
    )
    detect_parser.add_argument(
        "--emit-period",
        action="store_true",
        default=False,
        help="Print the most recent COMPLETE/RESTATED period with baseline available.",
    )

    # 2. `autopilot ingest --period 2026-Q2 [--as-of YYYY-MM-DD] [--dry-run]`
    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Ingest target period and lookback periods into fresh DB (data/autopilot.db).",
    )
    _add_common_flags(ingest_parser)
    ingest_parser.add_argument(
        "--period",
        type=str,
        required=True,
        help="Reporting period to ingest (e.g., 2026-Q2).",
    )

    # 3. `autopilot facts --period 2026-Q2 [--dry-run]`
    facts_parser = subparsers.add_parser(
        "facts",
        help="Compute deterministic FactsBundle (facts.json, diff.md, run.json) from SQLite DB.",
    )
    _add_common_flags(facts_parser)
    facts_parser.add_argument(
        "--period",
        type=str,
        required=True,
        help="Reporting period to compute facts for (e.g., 2026-Q2).",
    )

    # 4. `autopilot narrate --period 2026-Q2 [--dry-run] [--llm none]`
    narrate_parser = subparsers.add_parser(
        "narrate",
        help="Draft quarterly research note (reports/<period>/draft.md) from FactsBundle.",
    )
    _add_common_flags(narrate_parser)
    narrate_parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Reporting period to draft research note for (e.g., 2026-Q2).",
    )
    narrate_parser.add_argument(
        "--provider",
        "--llm",
        dest="llm_provider",
        type=str,
        choices=["null", "none", "manual", "gemini", "auto"],
        default=None,
        help="Override LLM provider selection (null/none, manual, gemini, auto).",
    )
    narrate_parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="Maximum validation retry attempts for narrative generation (default: 3).",
    )

    # 5. `autopilot validate --period 2026-Q2 [--draft path] [--dry-run]`
    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate research note (draft.md) against facts.json using rules R1-R8.",
    )
    _add_common_flags(validate_parser)
    validate_parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Reporting period to validate (e.g., 2026-Q2).",
    )
    validate_parser.add_argument(
        "--draft",
        type=str,
        default=None,
        help="Path to draft markdown file (default: reports/<period>/draft.md).",
    )

    # 6. `autopilot publish --period 2026-Q2 [--commit-state] [--replay] [--dry-run]`
    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish final.md, final_cited.md, CSV exports, PR body, and optional state commit.",
    )
    _add_common_flags(publish_parser)
    publish_parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Reporting period to publish (e.g., 2026-Q2).",
    )
    publish_parser.add_argument(
        "--commit-state",
        action="store_true",
        default=False,
        help="Mark the period's accessions as processed in state/processed_filings.json.",
    )
    publish_parser.add_argument(
        "--replay",
        action="store_true",
        default=False,
        help="Label outputs as replay and never modify state.",
    )

    # 7. `autopilot learn --period 2026-Q2 [--review-minutes M]`
    learn_parser = subparsers.add_parser(
        "learn",
        help="Compare merged final_cited.md with bot draft and append metrics/history.jsonl.",
    )
    _add_common_flags(learn_parser)
    learn_parser.add_argument(
        "--period",
        type=str,
        required=True,
        help="Reporting period to record feedback for (e.g., 2026-Q2).",
    )
    learn_parser.add_argument(
        "--review-minutes",
        type=float,
        default=None,
        help="Optional human review duration in minutes.",
    )

    # 8. `autopilot metrics [--markdown]`
    metrics_parser = subparsers.add_parser(
        "metrics",
        help="Render historical pipeline and feedback metrics table.",
    )
    _add_common_flags(metrics_parser)
    metrics_parser.add_argument(
        "--markdown",
        action="store_true",
        default=False,
        help="Output the Markdown results table for README.",
    )

    # 9. `autopilot run [--through STAGE] [--period YYYY-QN] [--as-of YYYY-MM-DD] [--dry-run]`
    run_parser = subparsers.add_parser(
        "run",
        help="Run pipeline stages in order (optionally stopping after --through <stage>).",
    )
    _add_common_flags(run_parser)
    run_parser.add_argument(
        "--through",
        type=str,
        default=None,
        help="Stop pipeline after executing this stage (detect..publish).",
    )
    run_parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Override target reporting period (e.g., 2026-Q2).",
    )
    run_parser.add_argument(
        "--provider",
        "--llm",
        dest="llm_provider",
        type=str,
        choices=["null", "none", "manual", "gemini", "auto"],
        default=None,
        help="Override LLM provider selection (null/none, manual, gemini, auto).",
    )
    run_parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="Maximum validation retry attempts for narrative generation (default: 3).",
    )
    run_parser.add_argument(
        "--commit-state",
        action="store_true",
        default=False,
        help="Commit period accessions to state/processed_filings.json during publish.",
    )
    run_parser.add_argument(
        "--replay",
        action="store_true",
        default=False,
        help="Run in replay mode (labels outputs replay and never modifies state).",
    )

    # 10. `autopilot seed-state --through 2026-Q2 [--yes]`
    seed_state_parser = subparsers.add_parser(
        "seed-state",
        help="Mark accessions through a period as processed (dry-run by default; --yes to write).",
    )
    _add_common_flags(seed_state_parser)
    seed_state_parser.add_argument(
        "--through",
        type=str,
        required=True,
        help="Mark all filing accessions up through this period (e.g., 2026-Q2).",
    )
    seed_state_parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Apply state write to state/processed_filings.json (default is dry-run).",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `autopilot` CLI command and return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_env(config_path=args.config)
    explicit_period = getattr(args, "period", None)
    if explicit_period:
        settings.target_period = str(explicit_period)
        settings.baseline_period = compute_previous_period(settings.target_period)
    if getattr(args, "llm_provider", None):
        raw_prov = str(args.llm_provider).lower()
        settings.llm_provider = "null" if raw_prov == "none" else raw_prov
    if getattr(args, "max_attempts", None) is not None:
        settings.max_validation_retries = int(args.max_attempts)

    configure_logging(settings)
    logger.info("Loaded settings: %s", settings)

    cmd = getattr(args, "command", None)
    as_of = getattr(args, "as_of", None)
    dry_run = bool(getattr(args, "dry_run", False))

    if cmd == "detect":
        emit_period = bool(getattr(args, "emit_period", False))
        detector = DetectStage(settings=settings)
        detect_res = detector.detect(
            target_period=str(explicit_period) if explicit_period else None,
            as_of=as_of,
            dry_run=dry_run or emit_period,
            write_reports=not (dry_run or emit_period),
            include_config_target=not emit_period or bool(explicit_period),
        )
        if emit_period:
            chosen_period = select_actionable_period(detect_res)
            out_line = chosen_period if chosen_period else "no actionable period"
            sys.stdout.write(f"{out_line}\n")
            return 0

        for p_name, p_obj in sorted(detect_res.periods.items()):
            logger.info(
                "Detect [%s] status=%s actionable=%s filings=%d new=%d reason=%s",
                p_name,
                p_obj.status,
                p_obj.actionable,
                len(p_obj.filings),
                len(p_obj.new_filings),
                p_obj.reason,
            )
        if detect_res.noop_reason:
            logger.info("Detect no-op reason: %s", detect_res.noop_reason)
        return 0

    if cmd == "seed-state":
        apply_writes = bool(getattr(args, "yes", False)) and not dry_run
        _, summary_text = seed_state_through(
            state_path=settings.state_dir / "processed_filings.json",
            through_period=str(args.through),
            apply_writes=apply_writes,
            seed_dir=settings.seed_dir,
            reports_dir=settings.reports_dir,
        )
        sys.stdout.write(f"{summary_text}\n")
        return 0

    if cmd == "ingest":
        ingestor = IngestStage(settings=settings)
        ingest_res = ingestor.ingest(
            target_period=settings.target_period,
            as_of=as_of,
            dry_run=dry_run,
            write_report=not dry_run,
        )
        logger.info(
            "Ingest [%s] db=%s rows=%d all_reconciled=%s confirmed_splits=%d "
            "unconfirmed_splits=%d amendments=%d",
            " + ".join(ingest_res.loaded_periods),
            ingest_res.db_path,
            ingest_res.holdings_row_count,
            ingest_res.all_reconciled,
            len(ingest_res.confirmed_splits),
            len(ingest_res.unconfirmed_splits),
            len(ingest_res.amendments_applied),
        )
        return 0 if ingest_res.all_reconciled else 1

    if cmd == "facts":
        facts_stage = FactsStage(settings=settings)
        facts_bundle = facts_stage.build_facts(
            target_period=settings.target_period,
            dry_run=dry_run,
            write_reports=not dry_run,
        )
        logger.info(
            "Facts [%s] prev=%s fact_count=%d signals=%d discretionary=%d "
            "aum_equity_usd=%.0f report=%s diff=%s",
            facts_bundle.period,
            facts_bundle.previous_period,
            len(facts_bundle.facts),
            facts_bundle.signal_count,
            facts_bundle.discretionary_signal_count,
            facts_bundle.total_aum_equity_usd,
            facts_bundle.report_path,
            facts_bundle.diff_path,
        )
        return 0

    if cmd == "narrate":
        narrator = NarrateStage(settings=settings)
        ctx = PipelineContext(
            target_period=settings.target_period,
            baseline_period=settings.baseline_period,
            as_of=as_of,
            dry_run=dry_run,
        )
        res = narrator.run(ctx)
        return 0 if (isinstance(res, StageResult) and res.status == StageStatus.SUCCEEDED) else 1

    if cmd == "validate":
        validator = ValidateStage(settings=settings)
        ctx = PipelineContext(
            target_period=settings.target_period,
            baseline_period=settings.baseline_period,
            as_of=as_of,
            dry_run=dry_run,
        )
        draft_arg = getattr(args, "draft", None)
        if draft_arg:
            draft_p = Path(draft_arg)
            if draft_p.exists():
                ctx.draft = NarrativeDraft(
                    period=settings.target_period,
                    markdown=draft_p.read_text(encoding="utf-8"),
                )
        try:
            res = validator.run(ctx)
            return (
                0 if (isinstance(res, StageResult) and res.status == StageStatus.SUCCEEDED) else 1
            )
        except StageError as exc:
            logger.error("Validation error: %s", exc)
            return 1

    commit_state = bool(getattr(args, "commit_state", False))
    replay = bool(getattr(args, "replay", False))

    if cmd == "publish":
        publisher = PublishStage(
            settings=settings,
            commit_state=commit_state,
            replay=replay,
        )
        ctx = PipelineContext(
            target_period=settings.target_period,
            baseline_period=settings.baseline_period,
            as_of=as_of,
            dry_run=dry_run,
            commit_state=commit_state,
            replay=replay,
        )
        try:
            res = publisher.run(ctx)
            return (
                0 if (isinstance(res, StageResult) and res.status == StageStatus.SUCCEEDED) else 1
            )
        except StageError as exc:
            logger.error("Publish error: %s", exc)
            return 1

    if cmd == "learn":
        review_minutes = getattr(args, "review_minutes", None)
        learn_from_period(
            settings=settings,
            period=settings.target_period,
            review_minutes=review_minutes,
        )
        return 0

    if cmd == "metrics":
        table_md = format_metrics_markdown()
        if getattr(args, "markdown", False):
            sys.stdout.write(table_md + "\n")
        else:
            logger.info("Metrics table:\n%s", table_md)
        return 0

    # Default or `autopilot run`
    through_stage = getattr(args, "through", None)
    stages = build_default_stages(settings)
    if dry_run and cmd is None and through_stage is None:
        stage_names = [s.name for s in stages]
        logger.info("Dry-run stage order: %s", " -> ".join(stage_names))
        return 0

    pipeline = Pipeline(stages=stages, settings=settings)
    context = PipelineContext(
        target_period=settings.target_period,
        baseline_period=settings.baseline_period,
        as_of=as_of,
        dry_run=dry_run,
        commit_state=commit_state,
        replay=replay,
    )
    run_log = pipeline.run(
        context,
        persist_path=settings.state_dir / "last_run.json",
        through_stage=through_stage,
    )
    return 0 if run_log.status in {StageStatus.SUCCEEDED, StageStatus.AWAITING_REVIEW} else 1


if __name__ == "__main__":
    sys.exit(main())
