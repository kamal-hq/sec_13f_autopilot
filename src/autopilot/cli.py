"""Command-line entrypoint for `sec_13f_autopilot` (uses logging only, never `print`)."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from autopilot.models import PipelineContext, StageStatus
from autopilot.pipeline import Pipeline, build_default_stages
from autopilot.settings import Settings, configure_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="autopilot",
        description="Human-approved, one-click quarterly SEC 13F update pipeline.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to non-secret YAML configuration file (default: config.yaml).",
    )
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and log stage plan without executing stages.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `autopilot` CLI command and return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_env(config_path=args.config)
    if args.period:
        settings.target_period = args.period
    if args.provider:
        settings.llm_provider = args.provider

    configure_logging(settings)
    logger.info("Loaded settings: %s", settings)

    stages = build_default_stages()
    if args.dry_run:
        stage_names = [s.name for s in stages]
        logger.info("Dry-run stage order: %s", " -> ".join(stage_names))
        return 0

    pipeline = Pipeline(stages=stages, settings=settings)
    context = PipelineContext(
        target_period=settings.target_period,
        baseline_period=settings.baseline_period,
        dry_run=args.dry_run,
    )
    run_log = pipeline.run(
        context,
        persist_path=settings.state_dir / "last_run.json",
    )
    return 0 if run_log.status in {StageStatus.SUCCEEDED, StageStatus.AWAITING_REVIEW} else 1


if __name__ == "__main__":
    sys.exit(main())
