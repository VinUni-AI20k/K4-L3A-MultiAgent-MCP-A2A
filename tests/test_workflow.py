from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import ToolSpec
from student_agent.trace import TraceWriter
from student_agent.workflow import AgentModels, solve_case

TOOL_PARAMETERS = {
    "get_order": "order_id",
    "get_order_items": "order_id",
    "get_order_payments": "order_id",
    "get_payment_timeline": "order_id",
    "get_refund_timeline": "order_id",
    "get_shipment_summary": "order_id",
    "get_sellers": "order_id",
    "get_policy": "policy_version",
}


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.tools = [
            ToolSpec(
                name=name,
                description=f"Authoritative {name} evidence",
                input_schema={
                    "type": "object",
                    "required": ["case_id", parameter],
                    "properties": {
                        "case_id": {"type": "string"},
                        parameter: {"type": "string"},
                    },
                },
            )
            for name, parameter in TOOL_PARAMETERS.items()
        ]

    async def get_tools(self) -> list[ToolSpec]:
        return self.tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        suffix = (tool_name.replace("get_", "") + "_0123456789abcdefghijklmnop")[:32]
        domain = "policy" if tool_name == "get_policy" else "order"
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": domain,
            "data": {"tool": tool_name, **arguments},
        }


class FakeLLM:
    def __init__(self, revision_once: bool = False) -> None:
        self.revision_once = revision_once
        self.verifier_calls = 0

    async def complete_json(self, **request: Any) -> dict[str, Any]:
        name, payload = request["schema_name"], request["payload"]
        if name == "coordinator_plan":
            return {
                "claim_ids": [
                    claim["claim_id"]
                    for claim in payload["claims"]
                ],
                "investigation_focus": ["order_payment", "policy_resolution"],
                "risk_flags": [],
            }
        if name.endswith("_finding"):
            refs = [item["evidence_ref"] for item in payload["evidence"]]
            return {
                "summary": "Evidence reviewed",
                "claim_findings": [],
                "entities": {key: [] for key in (
                    "order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids"
                )},
                "candidate_issue": "duplicate_charge",
                "root_causes": ["DUPLICATE_PAYMENT_CAPTURE"],
                "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
                "refund_lines": [],
                "resolution_actions": [],
                "data_conflicts": [],
                **({} if refs else {}),
            }
        self.verifier_calls += 1
        case = payload["case"]
        refs = payload["available_evidence_refs"]
        revise = self.revision_once and self.verifier_calls == 1
        return {
            "decision": {
                "assessment": {
                    "primary_issue": "duplicate_charge",
                    "case_status": "action_required",
                    "confidence": 0.91,
                },
                "affected_entities": {
                    "order_ids": [case["customer_request"]["claimed_order_id"]],
                    "item_ids": [],
                    "seller_ids": [],
                    "payment_references": ["payment-1"],
                    "shipment_ids": [],
                },
                "claim_assessments": [
                    {
                        "claim_id": claim["claim_id"],
                        "verdict": "supported",
                        "confidence": 0.9,
                        "evidence_refs": refs[:2],
                    }
                    for claim in case["customer_request"]["claims"]
                ],
                "root_cause_analysis": {
                    "ranked_causes": [{"cause_code": "DUPLICATE_PAYMENT_CAPTURE", "rank": 1}],
                    "responsible_parties": [
                        {"party_type": "payment_provider", "party_id": None}
                    ],
                },
                "evidence_refs": refs,
                "data_conflicts": [],
                "financial_resolution": {
                    "currency": "BRL",
                    "recommended_refund_brl": 10.0,
                    "refund_lines": [
                        {
                            "reason_code": "DUPLICATE_CHARGE",
                            "amount_brl": 10.0,
                            "entity_id": "payment-1",
                        }
                    ],
                },
                "resolution_actions": ["ISSUE_REFUND"],
            },
            "revision_required": revise,
            "revision_target": "shipment-seller-agent" if revise else None,
            "revision_reason": "Recheck responsibility" if revise else None,
        }


def sample_case() -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_TEST",
        "opened_at": "2018-01-01T00:00:00Z",
        "customer_request": {
            "language": "vi",
            "message": "Khách hàng báo bị trừ tiền hai lần.",
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-1", "topic": "duplicate_charge"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


@pytest.mark.parametrize("revision_once", [False, True])
def test_workflow_is_bounded_and_contract_valid(
    tmp_path: Path, revision_once: bool
) -> None:
    asyncio.run(_exercise_workflow(tmp_path, revision_once))


async def _exercise_workflow(tmp_path: Path, revision_once: bool) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway, llm, case = FakeGateway(), FakeLLM(revision_once), sample_case()
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = await solve_case(
        case,
        gateway,  # type: ignore[arg-type]
        trace,
        llm=llm,
        models=AgentModels("coordinator", "specialist", "verifier"),
    )
    contracts.validate_output(output, "test output")
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    event_types = {event["event_type"] for event in events}
    assert {
        "case_received", "task_assigned", "tool_result_consumed", "handoff",
        "policy_decided", "verification_completed", "case_finalized",
    } <= event_types
    assert all(call[1] == case["case_id"] for call in gateway.calls)
    assert len(gateway.calls) == len({call[0] for call in gateway.calls})
    assert llm.verifier_calls == (2 if revision_once else 1)
    assert output["financial_resolution"]["recommended_refund_brl"] == sum(
        line["amount_brl"] for line in output["financial_resolution"]["refund_lines"]
    )
