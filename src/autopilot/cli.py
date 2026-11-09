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

from autopilot.models import PipelineContext, StageStatus
from autopilot.pipeline import Pipeline, build_default_stages
from autopilot.settings import Settings, configure_logging
from autopilot.stages.detect import DetectStage
from autopilot.stages.facts import FactsStage
from autopilot.stages.ingest import IngestStage

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
        type=str,
        choices=["null", "manual", "gemini", "auto"],
        default=None,
        help="Override LLM provider selection.",
    )

    subparsers = parser.add_subparsers(dest="command")

    # 1. `autopilot detect [--as-of YYYY-MM-DD] [--dry-run]`
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

    # 4. `autopilot run [--through STAGE] [--period YYYY-QN] [--as-of YYYY-MM-DD] [--dry-run]`
    run_parser = subparsers.add_parser(
        "run",
        help="Run pipeline stages in order (optionally stopping after --through <stage>).",
    )
    _add_common_flags(run_parser)
    run_parser.add_argument(
        "--through",
        type=str,
        default=None,
        help="Stop pipeline after executing this stage (e.g., detect, ingest, facts).",
    )
    run_parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Override target reporting period (e.g., 2026-Q2).",
    )
    run_parser.add_argument(
        "--provider",
        type=str,
        choices=["null", "manual", "gemini", "auto"],
        default=None,
        help="Override LLM provider selection.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `autopilot` CLI command and return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_env(config_path=args.config)
    if getattr(args, "period", None):
        settings.target_period = str(args.period)
    if getattr(args, "provider", None):
        settings.llm_provider = str(args.provider)

    configure_logging(settings)
    logger.info("Loaded settings: %s", settings)

    cmd = getattr(args, "command", None)
    as_of = getattr(args, "as_of", None)
    dry_run = bool(getattr(args, "dry_run", False))

    if cmd == "detect":
        detector = DetectStage(settings=settings)
        detect_res = detector.detect(
            target_period=settings.target_period,
            as_of=as_of,
            dry_run=dry_run,
            write_reports=not dry_run,
        )
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
    )
    run_log = pipeline.run(
        context,
        persist_path=settings.state_dir / "last_run.json",
        through_stage=through_stage,
    )
    return 0 if run_log.status in {StageStatus.SUCCEEDED, StageStatus.AWAITING_REVIEW} else 1


if __name__ == "__main__":
    sys.exit(main())
