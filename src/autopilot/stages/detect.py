"""DetectStage: polls SEC EDGAR submissions via `SecGateway` and evaluates period completeness.

Stage Contract
--------------
- **Stage Name**: ``detect``
- **Input Contract**:
  - ``context.target_period`` (``str``, e.g., ``"2026-Q2"``): Target reporting quarter.
  - ``context.as_of`` (``str | None``, e.g., ``"2026-08-20"``): Optional replay cutoff date
    (`YYYY-MM-DD`). Any filing with ``filed_date > as_of`` is ignored.
  - ``settings.funds`` (``dict[str, str]``): Mapping of institutional fund names to 10-digit CIKs.
  - ``StateStore`` (``state/processed_filings.json``): Read-only lookup of already processed
    accessions. ``DetectStage`` NEVER writes to ``state/processed_filings.json``.
  - ``SecGateway``: Rate-limited SEC EDGAR gateway (<= 8 req/s).
- **Output Contract**:
  - Writes ``context.detection`` as a :class:`autopilot.models.DetectResult` and persists
    ``reports/<period>/detect.json`` (unless ``context.dry_run`` is ``True``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from autopilot.gateways.sec import SecConfigurationError, SecGateway
from autopilot.models import (
    DetectResult,
    Filing,
    Period,
    PeriodStatus,
    PipelineContext,
    StageStatus,
)
from autopilot.policies import PeriodCompletenessPolicy
from autopilot.settings import Settings
from autopilot.stages.base import Stage, StageResult
from autopilot.state import StateStore

logger = logging.getLogger(__name__)


def _resolve_sec_gateway(settings: Settings, sec_gateway: SecGateway | None) -> SecGateway:
    """Return provided `SecGateway` or construct one (falling back to local fixtures if needed)."""
    if sec_gateway is not None:
        return sec_gateway

    data_sub = settings.data_dir / "submissions"
    test_fix = Path("tests/fixtures")
    if data_sub.exists() and (not settings.sec_user_agent or "@" not in settings.sec_user_agent):
        return SecGateway.from_fixtures(
            settings,
            fixtures_dir=data_sub,
            raw_xml_dirs=[settings.data_dir / "raw", test_fix / "raw"],
        )
    try:
        return SecGateway(settings)
    except SecConfigurationError:
        if data_sub.exists():
            return SecGateway.from_fixtures(
                settings,
                fixtures_dir=data_sub,
                raw_xml_dirs=[settings.data_dir / "raw", test_fix / "raw"],
            )
        if test_fix.exists():
            return SecGateway.from_fixtures(settings, fixtures_dir=test_fix)
        raise


class DetectStage(Stage):
    """Detects new 13F-HR and 13F-HR/A filings across configured funds and evaluates periods."""

    name: str = "detect"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        sec_gateway: SecGateway | None = None,
        state_store: StateStore | None = None,
        min_period: str = "2026-Q1",
    ) -> None:
        self._settings = settings or Settings()
        self._sec_gateway = sec_gateway
        self._state_store = state_store
        self.min_period = min_period

    def detect(
        self,
        *,
        target_period: str | None = None,
        as_of: str | None = None,
        dry_run: bool = False,
        write_reports: bool = True,
    ) -> DetectResult:
        """Fetch submissions for configured funds, evaluate periods, and build `DetectResult`."""
        resolved_target = target_period or self._settings.target_period
        gateway = _resolve_sec_gateway(self._settings, self._sec_gateway)
        store = self._state_store or StateStore(self._settings.state_dir / "processed_filings.json")
        store.load()
        processed_set = store.processed_accessions(as_of=as_of)

        all_candidates: list[Filing] = []
        for fund_name, cik in self._settings.funds.items():
            padded_cik = cik.strip().zfill(10)
            fund_filings = gateway.get_latest_13f_metadata(
                padded_cik,
                fund_name=fund_name,
                as_of=as_of,
                min_period=self.min_period,
                inspect_cover_pages=True,
            )
            all_candidates.extend(fund_filings)

        # Determine all periods to evaluate
        discovered_periods: set[str] = {f.period for f in all_candidates}
        for rec in store.records.values():
            if rec.period and (as_of is None or not rec.filed_date or rec.filed_date <= as_of):
                discovered_periods.add(rec.period)
        if resolved_target:
            discovered_periods.add(resolved_target)

        policy = PeriodCompletenessPolicy(
            configured_funds=self._settings.funds,
            min_funds=self._settings.min_funds,
            allow_partial=self._settings.allow_partial,
        )

        periods_map: dict[str, Period] = {}
        actionable_periods: list[str] = []
        for period_name in sorted(discovered_periods):
            period_obj = policy.evaluate_period(
                period_name,
                all_candidates,
                processed_set,
            )
            periods_map[period_name] = period_obj
            if period_obj.actionable:
                actionable_periods.append(period_name)

        target_obj = periods_map.get(resolved_target)
        new_filings = list(target_obj.new_filings) if target_obj is not None else []
        already_ingested = list(target_obj.already_processed) if target_obj is not None else []
        has_updates = len(actionable_periods) > 0

        noop_reason: str | None = None
        if not has_updates:
            review_periods = [
                p.period for p in periods_map.values() if p.status == PeriodStatus.NEEDS_REVIEW
            ]
            partial_periods = [
                p.period for p in periods_map.values() if p.status == PeriodStatus.PARTIAL
            ]
            if review_periods:
                reasons = "; ".join(periods_map[p].reason for p in review_periods)
                noop_reason = (
                    f"No auto-processable updates: period(s) {', '.join(review_periods)} "
                    f"require manual review ({reasons})."
                )
            elif partial_periods:
                reasons = "; ".join(periods_map[p].reason for p in partial_periods)
                noop_reason = f"No complete periods ready to process: {reasons}"
            else:
                as_of_note = f" as of {as_of}" if as_of else ""
                noop_reason = (
                    f"No new 13F filings detected{as_of_note}; all evaluated periods "
                    f"({', '.join(sorted(periods_map))}) are already PROCESSED or PENDING."
                )

        result = DetectResult(
            period=resolved_target,
            as_of=as_of,
            periods=periods_map,
            new_filings=new_filings,
            already_ingested=already_ingested,
            actionable_periods=actionable_periods,
            has_updates=has_updates,
            noop_reason=noop_reason,
        )

        if write_reports and not dry_run:
            # Write reports/<period>/detect.json for target period and any actionable period
            periods_to_write = set(actionable_periods) | {resolved_target}
            for p_name in sorted(periods_to_write):
                report_dir = self._settings.reports_dir / p_name
                report_dir.mkdir(parents=True, exist_ok=True)
                report_path = report_dir / "detect.json"
                payload = result.to_dict()
                payload["focused_period"] = (
                    periods_map[p_name].to_dict() if p_name in periods_map else None
                )
                report_path.write_text(
                    json.dumps(payload, indent=2) + "\n",
                    encoding="utf-8",
                )
                result.report_paths[p_name] = str(report_path)
                logger.info("Wrote detect report to %s", report_path)
        elif dry_run:
            logger.info("Dry-run: skipping detect.json report write")

        return result

    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute filing detection and attach `DetectResult` to `context.detection`."""
        detect_result = self.detect(
            target_period=context.target_period,
            as_of=context.as_of,
            dry_run=context.dry_run,
            write_reports=True,
        )
        context.detection = detect_result

        target_period_obj = detect_result.get_period(context.target_period)
        target_status = (
            str(target_period_obj.status) if target_period_obj is not None else "UNKNOWN"
        )
        msg = (
            f"period={context.target_period} status={target_status} "
            f"has_updates={detect_result.has_updates}"
        )
        if detect_result.noop_reason:
            msg = f"{msg} reason={detect_result.noop_reason}"

        return StageResult(
            status=StageStatus.SUCCEEDED,
            message=msg,
            metadata={
                "target_period": context.target_period,
                "period_status": target_status,
                "has_updates": detect_result.has_updates,
                "actionable_periods": detect_result.actionable_periods,
            },
        )
