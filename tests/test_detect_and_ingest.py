"""Offline unit tests for Phase 1: StateStore, CompletenessPolicy, DetectStage, and IngestStage."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from autopilot.cli import main as cli_main
from autopilot.gateways.sec import SecGateway
from autopilot.models import Filing, PeriodStatus, PipelineContext, StageStatus
from autopilot.pipeline import Pipeline
from autopilot.policies import PeriodCompletenessPolicy
from autopilot.settings import DEFAULT_FUNDS, Settings
from autopilot.stages.base import StageError
from autopilot.stages.detect import DetectStage
from autopilot.stages.ingest import IngestStage
from autopilot.state import StateStore

FIXTURES_DIR = Path("tests/fixtures")

Q1_PROCESSED_ACCESSIONS: dict[str, tuple[str, str]] = {
    "0001193125-26-226661": ("2026-Q1", "2026-05-15"),
    "0001037389-26-000033": ("2026-Q1", "2026-05-13"),
    "0001104659-26-062477": ("2026-Q1", "2026-05-15"),
    "0000899140-26-000547": ("2026-Q1", "2026-05-15"),
    "0001350694-26-000002": ("2026-Q1", "2026-05-14"),
    "0000919079-26-000006": ("2026-Q1", "2026-05-12"),
    "0001021944-26-000011": ("2026-Q1", "2026-05-14"),
}


def _seed_q1_state(state_path: Path) -> StateStore:
    """Create a StateStore pre-populated with all 7 funds' 2026-Q1 filings."""
    store = StateStore(state_path)
    for acc, (period, filed_date) in Q1_PROCESSED_ACCESSIONS.items():
        store.record_filing(
            acc,
            period=period,
            filed_date=filed_date,
            processed_at="2026-05-20T00:00:00+00:00",
            supersedes=None,
        )
    store.save()
    return store


def test_state_store_roundtrip_and_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StateStore writes atomically via os.replace and cleans up temp files on failure."""
    state_file = tmp_path / "state" / "processed_filings.json"
    _seed_q1_state(state_file)
    assert state_file.exists()

    reloaded = StateStore(state_file)
    assert reloaded.schema_version == 1
    assert len(reloaded.records) == 7
    assert reloaded.is_processed("0001193125-26-226661")

    # Dry-run save must not write new records to disk
    reloaded.record_filing(
        "0001193125-26-352200",
        period="2026-Q2",
        filed_date="2026-08-14",
    )
    reloaded.save(dry_run=True)
    disk_after_dry = StateStore(state_file)
    assert not disk_after_dry.is_processed("0001193125-26-352200")

    # Simulated failure in os.replace leaves existing file intact and removes .tmp file
    original_bytes = state_file.read_bytes()

    def _boom_replace(src: object, dst: object) -> None:
        raise OSError("Simulated atomic rename failure")

    monkeypatch.setattr(os, "replace", _boom_replace)
    with pytest.raises(OSError, match="Simulated atomic rename failure"):
        reloaded.save(dry_run=False)

    assert state_file.read_bytes() == original_bytes
    tmp_leftovers = list(state_file.parent.glob("*.tmp"))
    assert tmp_leftovers == []


def test_completeness_policy_complete_partial_and_min_funds() -> None:
    """Completeness policy handles PENDING, PARTIAL (with allow_partial/min_funds), and COMPLETE."""
    funds = {
        "Fund A": "0000000001",
        "Fund B": "0000000002",
        "Fund C": "0000000003",
    }
    strict_policy = PeriodCompletenessPolicy(
        configured_funds=funds,
        min_funds=2,
        allow_partial=False,
    )

    # 0 filings -> PENDING
    p_pending = strict_policy.evaluate_period("2026-Q2", [], set())
    assert p_pending.status == PeriodStatus.PENDING
    assert p_pending.actionable is False

    # 2 of 3 filings with allow_partial=False -> PARTIAL and not actionable
    two_filings = [
        Filing("0000000001", "Fund A", "0000000001-26-000001", "13F-HR", "2026-08-14", "2026-Q2"),
        Filing("0000000002", "Fund B", "0000000002-26-000001", "13F-HR", "2026-08-14", "2026-Q2"),
    ]
    p_partial_strict = strict_policy.evaluate_period("2026-Q2", two_filings, set())
    assert p_partial_strict.status == PeriodStatus.PARTIAL
    assert p_partial_strict.actionable is False
    assert p_partial_strict.missing_funds == ["Fund C"]

    # 2 of 3 filings with allow_partial=True and min_funds=2 -> PARTIAL and actionable=True
    partial_ok_policy = PeriodCompletenessPolicy(
        configured_funds=funds,
        min_funds=2,
        allow_partial=True,
    )
    p_partial_allowed = partial_ok_policy.evaluate_period("2026-Q2", two_filings, set())
    assert p_partial_allowed.status == PeriodStatus.PARTIAL
    assert p_partial_allowed.actionable is True

    # 3 of 3 filings -> COMPLETE and actionable=True
    all_three = [
        *two_filings,
        Filing("0000000003", "Fund C", "0000000003-26-000001", "13F-HR", "2026-08-14", "2026-Q2"),
    ]
    p_complete = strict_policy.evaluate_period("2026-Q2", all_three, set())
    assert p_complete.status == PeriodStatus.COMPLETE
    assert p_complete.actionable is True
    assert p_complete.missing_funds == []


def test_detect_replay_as_of_2026_08_20_vs_2026_09_05_and_idempotency(
    tmp_path: Path,
) -> None:
    """Replay detect at 2026-08-20 (Citadel original) vs 2026-09-05 (Citadel RESTATEMENT)."""
    state_path = tmp_path / "state" / "processed_filings.json"
    reports_dir = tmp_path / "reports"
    store = _seed_q1_state(state_path)
    state_mtime_before = state_path.stat().st_mtime_ns

    settings = Settings(
        sec_user_agent="TestAgent test@institution.org",
        state_dir=tmp_path / "state",
        reports_dir=reports_dir,
        funds=dict(DEFAULT_FUNDS),
    )
    gateway = SecGateway.from_fixtures(settings, fixtures_dir=FIXTURES_DIR)
    detector = DetectStage(settings=settings, sec_gateway=gateway, state_store=store)

    # 1. Detect as-of 2026-08-20: Q1 is PROCESSED, Q2 is COMPLETE (Citadel 0001104659-26-097200)
    res_aug = detector.detect(target_period="2026-Q2", as_of="2026-08-20")
    assert res_aug.has_updates is True
    assert res_aug.actionable_periods == ["2026-Q2"]
    assert res_aug.periods["2026-Q1"].status == PeriodStatus.PROCESSED
    q2_aug = res_aug.periods["2026-Q2"]
    assert q2_aug.status == PeriodStatus.COMPLETE
    assert len(q2_aug.filings) == 7
    assert len(q2_aug.new_filings) == 7
    citadel_aug = next(f for f in q2_aug.filings if f.cik == "0001423053")
    assert citadel_aug.accession == "0001104659-26-097200"
    assert citadel_aug.form_type == "13F-HR"
    assert citadel_aug.filed_date == "2026-08-14"
    assert citadel_aug.amendment_type is None
    assert (reports_dir / "2026-Q2" / "detect.json").exists()
    # DetectStage must NEVER write state
    assert state_path.stat().st_mtime_ns == state_mtime_before

    # 2. Simulate Q2 processed after 2026-08-20 (recording all 7 Q2 filings)
    for filing in q2_aug.filings:
        store.record_filing(
            filing.accession,
            period="2026-Q2",
            filed_date=filing.filed_date,
            processed_at="2026-08-21T00:00:00+00:00",
        )
    store.save()

    # Re-running at 2026-08-20 is now an idempotent no-op!
    res_aug_noop = detector.detect(target_period="2026-Q2", as_of="2026-08-20")
    assert res_aug_noop.has_updates is False
    assert res_aug_noop.periods["2026-Q2"].status == PeriodStatus.PROCESSED
    assert res_aug_noop.noop_reason is not None
    assert "PROCESSED" in res_aug_noop.noop_reason

    # 3. Advance replay clock to 2026-09-05: Citadel's 13F-HR/A RESTATEMENT (0001104659-26-104387,
    # filed 2026-09-02) arrives and transitions 2026-Q2 from PROCESSED -> RESTATED with changelog!
    res_sep = detector.detect(target_period="2026-Q2", as_of="2026-09-05")
    assert res_sep.has_updates is True
    assert res_sep.actionable_periods == ["2026-Q2"]
    q2_sep = res_sep.periods["2026-Q2"]
    assert q2_sep.status == PeriodStatus.RESTATED
    assert len(q2_sep.new_filings) == 1
    restated_filing = q2_sep.new_filings[0]
    assert restated_filing.accession == "0001104659-26-104387"
    assert restated_filing.form_type == "13F-HR/A"
    assert restated_filing.amendment_type == "RESTATEMENT"
    assert restated_filing.filed_date == "2026-09-02"
    assert restated_filing.supersedes == ("0001104659-26-097200",)
    assert q2_sep.superseded_accessions == {"0001104659-26-104387": ["0001104659-26-097200"]}
    assert any(
        "0001104659-26-104387" in entry and "0001104659-26-097200" in entry
        for entry in q2_sep.changelog
    )


def test_new_holdings_amendment_sets_needs_review_and_blocks_ingest(
    tmp_path: Path,
) -> None:
    """A 13F-HR/A NEW HOLDINGS amendment marks the period NEEDS_REVIEW and prevents auto-ingest."""
    custom_fix = tmp_path / "custom_fixtures"
    shutil.copytree(FIXTURES_DIR / "submissions", custom_fix / "submissions")
    cover_pages = json.loads((FIXTURES_DIR / "cover_pages.json").read_text(encoding="utf-8"))

    # Add a partial NEW HOLDINGS 13F-HR/A amendment for Berkshire Hathaway in 2026-Q2
    berk_sub_path = custom_fix / "submissions" / "CIK0001067983.json"
    berk_sub = json.loads(berk_sub_path.read_text(encoding="utf-8"))
    berk_sub["filings"]["recent"]["accessionNumber"].insert(0, "0001193125-26-399999")
    berk_sub["filings"]["recent"]["form"].insert(0, "13F-HR/A")
    berk_sub["filings"]["recent"]["filingDate"].insert(0, "2026-08-25")
    berk_sub["filings"]["recent"]["reportDate"].insert(0, "2026-06-30")
    berk_sub["filings"]["recent"]["primaryDocument"].insert(0, "primary_doc.xml")
    berk_sub_path.write_text(json.dumps(berk_sub, indent=2), encoding="utf-8")

    cover_pages["0001193125-26-399999"] = {
        "cik": "0001067983",
        "accession": "0001193125-26-399999",
        "primary_doc": "primary_doc.xml",
        "infotable_doc": "form13fInfoTable.xml",
        "is_amendment": True,
        "amendment_type": "NEW HOLDINGS",
        "table_entry_total": 5,
        "table_value_total": 1000000,
    }
    (custom_fix / "cover_pages.json").write_text(
        json.dumps(cover_pages, indent=2),
        encoding="utf-8",
    )

    _seed_q1_state(tmp_path / "state" / "processed_filings.json")
    settings = Settings(
        sec_user_agent="TestAgent test@institution.org",
        state_dir=tmp_path / "state",
        reports_dir=tmp_path / "reports",
        data_dir=tmp_path / "data",
    )
    gateway = SecGateway.from_fixtures(settings, fixtures_dir=custom_fix)
    detector = DetectStage(settings=settings, sec_gateway=gateway)
    res = detector.detect(target_period="2026-Q2", as_of="2026-09-05")

    q2 = res.periods["2026-Q2"]
    assert q2.status == PeriodStatus.NEEDS_REVIEW
    assert q2.actionable is False
    assert res.has_updates is False
    assert any("NEW HOLDINGS" in r for r in q2.review_reasons)

    ingestor = IngestStage(settings=settings, sec_gateway=gateway)
    with pytest.raises(StageError, match="NEEDS_REVIEW"):
        ingestor.ingest("2026-Q2", existing_detection=res)


def test_ingest_stage_and_cli_with_4_sample_xmls(tmp_path: Path) -> None:
    """IngestStage loads 2026-Q1 and 2026-Q2 using the 4 sample XMLs and reconciles."""
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    mappings_dir = data_dir / "mappings"
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    db_path = data_dir / "autopilot.db"

    shutil.copytree(FIXTURES_DIR / "raw", raw_dir)
    shutil.copytree(FIXTURES_DIR / "mappings", mappings_dir)
    shutil.copytree(FIXTURES_DIR / "submissions", data_dir / "submissions")
    shutil.copy2(FIXTURES_DIR / "cover_pages.json", data_dir / "submissions" / "cover_pages.json")

    two_funds = {
        "Berkshire Hathaway": "0001067983",
        "Temasek Holdings": "0001021944",
    }
    settings = Settings(
        sec_user_agent="TestAgent test@institution.org",
        target_period="2026-Q2",
        baseline_period="2026-Q1",
        min_funds=2,
        state_dir=state_dir,
        reports_dir=reports_dir,
        data_dir=data_dir,
        db_path=db_path,
        funds=two_funds,
    )
    gateway = SecGateway.from_fixtures(
        settings,
        fixtures_dir=FIXTURES_DIR,
        raw_xml_dirs=[raw_dir],
    )

    pipeline = Pipeline(
        stages=[
            DetectStage(settings=settings, sec_gateway=gateway),
            IngestStage(settings=settings, sec_gateway=gateway),
        ],
        settings=settings,
    )
    ctx = PipelineContext(target_period="2026-Q2", baseline_period="2026-Q1", as_of="2026-09-05")
    run_log = pipeline.run(ctx, through_stage="ingest")

    assert run_log.status == StageStatus.SUCCEEDED
    assert ctx.ingest is not None
    assert ctx.ingest.all_reconciled is True
    assert db_path.exists()
    assert (reports_dir / "2026-Q2" / "detect.json").exists()
    assert (reports_dir / "2026-Q2" / "ingest.json").exists()
    # Neither detect nor ingest writes processed_filings.json
    assert not (state_dir / "processed_filings.json").exists()

    # Check row counts and reconciliations across the 4 sample XMLs
    assert ctx.ingest.row_counts["Dim_Fund"] == 2
    assert len(ctx.ingest.reconciliations) == 4
    for rec in ctx.ingest.reconciliations:
        assert rec.reconciled is True
        assert rec.diff_usd == 0

    # Verify KLAC (10-for-1) and BKNG (25-for-1) splits detected and confirmed
    confirmed_cusips = {s.cusip for s in ctx.ingest.confirmed_splits}
    assert {"482480100", "09857L108"}.issubset(confirmed_cusips)

    # Also test CLI `detect --dry-run`
    cfg_path = tmp_path / "test_config.yaml"
    cfg_path.write_text(
        f"pipeline:\n"
        f"  target_period: '2026-Q2'\n"
        f"  baseline_period: '2026-Q1'\n"
        f"  min_funds: 2\n"
        f"  state_dir: '{state_dir.as_posix()}'\n"
        f"  reports_dir: '{reports_dir.as_posix()}'\n"
        f"  data_dir: '{data_dir.as_posix()}'\n"
        f"  db_path: '{db_path.as_posix()}'\n"
        f"funds:\n"
        f"  'Berkshire Hathaway': '0001067983'\n"
        f"  'Temasek Holdings': '0001021944'\n",
        encoding="utf-8",
    )
    exit_code = cli_main(
        ["detect", "--config", str(cfg_path), "--as-of", "2026-08-20", "--dry-run"]
    )
    assert exit_code == 0


@pytest.mark.live
def test_live_sec_edgar_submissions_smoke() -> None:
    """Live smoke test (skipped by default; run with `pytest -m live` and SEC_USER_AGENT set)."""
    settings = Settings.from_env()
    if not settings.sec_user_agent:
        pytest.skip("SEC_USER_AGENT not set in environment for live smoke test")
    gateway = SecGateway(settings)
    payload = gateway.get_submissions("0001067983")
    assert payload.get("cik") in {"1067983", "0001067983"}
