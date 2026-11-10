"""Adversarial test suite for ValidateStage (Phase 3b).

Loads tests/adversarial/cases.yaml and exercises:
- 10 bad mutations (each rejected by specified rule R1-R8)
- 5 good mutations (all accepted cleanly with status=PASS)
Logs summary line: 'bad rejected 10/10, good accepted 5/5'.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from autopilot.models import FactsBundle
from autopilot.stages.validate import ValidateStage

logger = logging.getLogger(__name__)


def _apply_mutation(draft_md: str, case: dict[str, Any]) -> str:
    """Apply mutation described in case dict to draft markdown."""
    action = case.get("action")
    if action == "truncate_at_target":
        target = case["target"]
        assert target in draft_md, f"Target '{target}' not found in draft"
        return draft_md.split(target)[0].strip() + "\n"

    if action == "swap_sector_and_data_quality":
        s1 = draft_md.find("## Sector View")
        s2 = draft_md.find("## Data Quality")
        s3 = draft_md.find("## Limitations")
        assert s1 != -1 and s2 != -1 and s3 != -1, "Required sections missing for swap"
        sector_part = draft_md[s1:s2]
        dq_part = draft_md[s2:s3]
        return draft_md[:s1] + dq_part + sector_part + draft_md[s3:]

    target = case.get("target")
    replacement = case.get("replacement")
    assert target is not None and replacement is not None
    assert target in draft_md, f"Target '{target}' not found in draft"
    return draft_md.replace(target, replacement, 1)


def test_adversarial_suite(caplog: Any) -> None:
    """Execute all 10 bad mutations and 5 good mutations from cases.yaml."""
    caplog.set_level(logging.INFO)
    cases_path = Path(__file__).parent / "adversarial" / "cases.yaml"
    assert cases_path.exists(), f"Missing {cases_path}"

    cases_data = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    bad_cases: list[dict[str, Any]] = cases_data.get("bad_cases", [])
    good_cases: list[dict[str, Any]] = cases_data.get("good_cases", [])

    facts_path = Path("reports/2026-Q2/facts.json")
    draft_path = Path("reports/2026-Q2/draft.md")
    assert facts_path.exists(), f"Missing facts.json at {facts_path}"
    assert draft_path.exists(), f"Missing draft.md at {draft_path}"

    facts = FactsBundle.from_file(facts_path)
    base_draft = draft_path.read_text(encoding="utf-8")
    stage = ValidateStage()

    bad_rejected = 0
    for case in bad_cases:
        case_id = case["id"]
        expected_rule = case["expected_rule"]
        mutated_md = _apply_mutation(base_draft, case)
        report = stage.validate_draft(mutated_md, facts, "2026-Q2")

        rule_ids = [issue.rule_id for issue in report.issues]
        assert report.status == "FAIL", f"Case {case_id} unexpectedly passed validation"
        assert expected_rule in rule_ids, (
            f"Case {case_id} failed with {rule_ids}, expected rule {expected_rule}"
        )
        bad_rejected += 1

    good_accepted = 0
    for case in good_cases:
        case_id = case["id"]
        mutated_md = _apply_mutation(base_draft, case)
        report = stage.validate_draft(mutated_md, facts, "2026-Q2")

        assert report.status == "PASS", (
            f"Good case {case_id} failed with issues: "
            f"{[f'{i.rule_id}: {i.expected}' for i in report.issues]}"
        )
        assert len(report.issues) == 0
        good_accepted += 1

    summary_msg = (
        f"bad rejected {bad_rejected}/{len(bad_cases)}, "
        f"good accepted {good_accepted}/{len(good_cases)}"
    )
    logger.info(summary_msg)
    assert summary_msg in caplog.text
    assert bad_rejected == 10
    assert good_accepted == 5
