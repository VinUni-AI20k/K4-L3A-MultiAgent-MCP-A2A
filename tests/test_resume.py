from __future__ import annotations

import json
from pathlib import Path

from student_agent.cli import _resume_cases
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


def _output(case_id: str) -> dict[str, object]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [], "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": [],
        },
        "claim_assessments": [],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": 0, "refund_lines": [],
        },
        "resolution_actions": [],
    }


def test_resume_keeps_only_valid_finalized_cases(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    outputs, traces = tmp_path / "outputs", tmp_path / "traces"
    outputs.mkdir()
    traces.mkdir()
    complete, incomplete = "L3A_CASE_001", "L3A_CASE_002"
    (outputs / f"{complete}.json").write_text(
        json.dumps(_output(complete)), encoding="utf-8"
    )
    (outputs / f"{incomplete}.json").write_text(
        json.dumps(_output(incomplete)), encoding="utf-8"
    )
    trace = TraceWriter(traces / "trace.jsonl", contracts)
    trace.emit(case_id=complete, event_type="case_received", actor="coordinator")
    trace.emit(case_id=complete, event_type="case_finalized", actor="coordinator")
    trace.emit(case_id=incomplete, event_type="case_received", actor="coordinator")

    assert _resume_cases(tmp_path, [complete, incomplete], contracts) == {complete}
    retained = [json.loads(line) for line in (traces / "trace.jsonl").read_text().splitlines()]
    assert {event["case_id"] for event in retained} == {complete}
