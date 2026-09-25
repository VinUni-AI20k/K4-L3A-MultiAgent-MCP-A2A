from __future__ import annotations

import json
from pathlib import Path

from student_agent.cli import _prepare_resume
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import _verifier_agent


def test_verifier_produces_contract_valid_output_from_noisy_agent_data() -> None:
    evidence_ref = "ev_" + "a" * 20
    order = {
        "evidence_refs": [evidence_ref],
        "analysis": {
            "order_ids": ["ORDER_1", "ORDER_1"],
            "item_ids": [],
            "seller_ids": [],
        },
    }
    payment = {
        "evidence_refs": [evidence_ref],
        "analysis": {
            "payment_references": [],
            "recommended_refund_brl": "not-a-number",
            "refund_lines": [],
        },
    }
    shipment = {
        "evidence_refs": [],
        "analysis": {"shipment_ids": []},
    }
    policy = {
        "evidence_refs": [],
        "analysis": {
            "primary_issue": "not-in-contract",
            "case_status": "not-in-contract",
            "confidence": 5,
            "responsible_parties": [{"party_type": "invalid", "party_id": 42}],
            "ranked_causes": [{"cause_code": "invalid cause", "rank": 99}],
            "claim_assessments": [],
            "data_conflicts": [],
            "resolution_actions": ["Investigate", "Investigate"],
        },
    }

    output = _verifier_agent(
        {"case_id": "CASE_001"}, order, payment, shipment, policy
    )

    root = Path(__file__).resolve().parents[1]
    Contracts(root / "contracts" / "schemas").validate_output(output, "test output")
    assert output["assessment"] == {
        "primary_issue": "insufficient_evidence",
        "case_status": "needs_investigation",
        "confidence": 1.0,
    }
    assert output["affected_entities"]["order_ids"] == ["ORDER_1"]
    assert output["evidence_refs"] == [evidence_ref]
    assert output["resolution_actions"] == ["Investigate"]


def test_resume_keeps_finalized_case_and_removes_partial_trace(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    empty_result = {"evidence_refs": [], "analysis": {}}
    policy = {
        "evidence_refs": [],
        "analysis": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.5,
        },
    }
    output = _verifier_agent(
        {"case_id": "CASE_001"},
        empty_result,
        empty_result,
        empty_result,
        policy,
    )
    (output_root / "CASE_001.json").write_text(
        json.dumps(output), encoding="utf-8"
    )
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")
    trace.emit(case_id="CASE_002", event_type="case_received", actor="coordinator")

    completed = _prepare_resume(
        ("CASE_001", "CASE_002"), output_root, trace_path, contracts
    )

    assert completed == {"CASE_001"}
    assert "CASE_002" not in trace_path.read_text(encoding="utf-8")
