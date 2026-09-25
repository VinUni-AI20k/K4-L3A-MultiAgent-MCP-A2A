from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from student_agent import VARIANT_ID
from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "CASE_001"
EVIDENCE_REF = "ev_abcdefghijklmnopqrstuvwxyz"


def output() -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": CASE_ID,
        "assessment": {
            "primary_issue": "canceled_order_paid",
            "case_status": "action_required",
            "confidence": 0.9,
        },
        "affected_entities": {
            "order_ids": ["ORDER-1"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "CANCELED_ORDER_PAID", "rank": 1}],
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        },
        "evidence_refs": [EVIDENCE_REF],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 79.0,
            "refund_lines": [
                {
                    "reason_code": "issue_refund",
                    "amount_brl": 79.0,
                    "entity_id": "ORDER-1",
                }
            ],
        },
        "resolution_actions": ["issue_refund"],
    }


def write_artifacts(root: Path, *, include_finalized: bool) -> None:
    (root / "outputs").mkdir(parents=True)
    (root / "outputs" / f"{CASE_ID}.json").write_text(
        json.dumps(output()), encoding="utf-8"
    )
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(root / "traces" / "trace.jsonl", contracts)
    events = [
        ("case_received", "coordinator", None, None),
        ("task_assigned", "coordinator", "order-agent", None),
        ("tool_result_consumed", "order-agent", "get_order", [EVIDENCE_REF]),
        ("handoff", "order-agent", "policy-agent", [EVIDENCE_REF]),
        ("policy_decided", "policy-agent", "canceled_order_paid", [EVIDENCE_REF]),
        ("verification_completed", "verifier", "l3a-output-v2", [EVIDENCE_REF]),
    ]
    if include_finalized:
        events.append(("case_finalized", "coordinator", None, None))
    for event_type, actor, target, evidence_refs in events:
        trace.emit(
            case_id=CASE_ID,
            event_type=event_type,
            actor=actor,
            target=target,
            tool_name="get_order" if event_type == "tool_result_consumed" else None,
            evidence_refs=evidence_refs,
        )


def case_set() -> CaseSet:
    return CaseSet("test-v1", VARIANT_ID, (CASE_ID,), {})


def test_validate_artifacts_enforces_complete_ordered_lifecycle(tmp_path: Path) -> None:
    write_artifacts(tmp_path, include_finalized=True)
    outputs, trace_lines = validate_artifacts(
        tmp_path, case_set(), Contracts(ROOT / "contracts" / "schemas")
    )
    assert outputs[CASE_ID]["case_id"] == CASE_ID
    assert len(trace_lines) == 7


def test_validate_artifacts_rejects_missing_case_finalized(tmp_path: Path) -> None:
    write_artifacts(tmp_path, include_finalized=False)
    with pytest.raises(ValueError, match="case_finalized"):
        validate_artifacts(tmp_path, case_set(), Contracts(ROOT / "contracts" / "schemas"))
