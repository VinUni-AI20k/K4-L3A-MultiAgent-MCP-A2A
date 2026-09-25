from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import ToolSpec
from student_agent.trace import TraceWriter
from student_agent.workflow import AgentModels, _active_specialists, _tool_plan, solve_case

TOOL_PARAMETERS = {
    "get_order": "order_id", "get_order_items": "order_id",
    "get_order_payments": "order_id", "get_payment_timeline": "order_id",
    "get_refund_timeline": "order_id", "get_shipment_summary": "order_id",
    "get_sellers": "order_id", "get_policy": "policy_version",
}


class FakeGateway:
    def __init__(self, delay: float = 0) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.tools = [
            ToolSpec(
                name=name,
                description=f"Authoritative {name} evidence",
                input_schema={
                    "type": "object", "required": ["case_id", parameter],
                    "properties": {
                        "case_id": {"type": "string"}, parameter: {"type": "string"},
                    },
                },
            )
            for name, parameter in TOOL_PARAMETERS.items()
        ]

    async def get_tools(self) -> list[ToolSpec]:
        return self.tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        suffix = (tool_name.replace("get_", "") + "_0123456789abcdefghijklmnop")[:32]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": "policy" if tool_name == "get_policy" else "order",
            "data": {
                "tool": tool_name, "order_id": "order-1",
                "payment_reference": "payment-1", **arguments,
            },
        }


class FakeLLM:
    def __init__(self, revision_once: bool = False, specialist_delay: float = 0) -> None:
        self.revision_once = revision_once
        self.specialist_delay = specialist_delay
        self.verifier_calls = 0
        self.calls: list[str] = []
        self.active_specialists = 0
        self.max_active_specialists = 0

    async def complete_json(self, **request: Any) -> dict[str, Any]:
        name, payload = request["schema_name"], request["payload"]
        self.calls.append(name)
        if name == "coordinator_plan":
            return {
                "claim_ids": [claim["claim_id"] for claim in payload["claims"]],
                "investigation_focus": ["order_payment", "policy_resolution"],
                "risk_flags": [], "requested_tools": [],
            }
        if name in {
            "order_payment_finding", "shipment_seller_finding", "policy_resolution_finding",
        }:
            self.active_specialists += 1
            self.max_active_specialists = max(
                self.max_active_specialists, self.active_specialists
            )
            await asyncio.sleep(self.specialist_delay)
            self.active_specialists -= 1
            refs = [item["evidence_ref"] for item in payload["evidence"]]
            if name == "order_payment_finding":
                return {
                    "summary": "Order evidence reviewed", "claim_findings": [],
                    "entities": _empty_entities(), "candidate_issue": "duplicate_charge",
                    "refund_lines": [], "data_conflicts": [], "evidence_refs": refs,
                }
            if name == "shipment_seller_finding":
                return {
                    "summary": "Shipment evidence reviewed", "entities": _empty_entities(),
                    "candidate_issue": "late_delivery_logistics",
                    "responsibility": "logistics_provider", "data_conflicts": [],
                    "evidence_refs": refs,
                }
            return {
                "summary": "Policy reviewed", "applicable_rules": ["REFUND_POLICY"],
                "eligible": "yes", "refund_cap_brl": 10.0,
                "resolution_actions": ["ISSUE_REFUND"], "evidence_refs": refs,
            }
        self.verifier_calls += 1
        case = payload["case"]
        refs = [item["evidence_ref"] for item in payload["available_evidence"]]
        revise = self.revision_once and self.verifier_calls == 1
        return {
            "decision": {
                "assessment": {
                    "primary_issue": "duplicate_charge", "case_status": "action_required",
                    "confidence": 0.91,
                },
                "affected_entities": {
                    **_empty_entities(), "order_ids": ["order-1"],
                    "payment_references": ["payment-1"],
                },
                "claim_assessments": [
                    {
                        "claim_id": claim["claim_id"], "verdict": "supported",
                        "confidence": 0.9, "evidence_refs": refs[:2],
                    }
                    for claim in case["customer_request"]["claims"]
                ],
                "root_cause_analysis": {
                    "ranked_causes": [{"cause_code": "DUPLICATE_PAYMENT_CAPTURE", "rank": 1}],
                    "responsible_parties": [
                        {"party_type": "payment_provider", "party_id": None}
                    ],
                },
                "evidence_refs": refs, "data_conflicts": [],
                "financial_resolution": {
                    "currency": "BRL", "recommended_refund_brl": 10.0,
                    "refund_lines": [{
                        "reason_code": "DUPLICATE_CHARGE", "amount_brl": 10.0,
                        "entity_id": "payment-1",
                    }],
                },
                "resolution_actions": ["ISSUE_REFUND"],
            },
            "revision_required": revise,
            "revision_target": "order-payment-agent" if revise else None,
            "revision_reason": "Fetch item evidence" if revise else None,
            "missing_tools": ["get_order_items"] if revise else [],
        }


def _empty_entities() -> dict[str, list[str]]:
    return {
        "order_ids": [], "item_ids": [], "seller_ids": [],
        "payment_references": [], "shipment_ids": [],
    }


def sample_case(topic: str = "duplicate_charge") -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_TEST", "opened_at": "2018-01-01T00:00:00Z",
        "customer_request": {
            "language": "vi", "message": "Khách hàng báo bị trừ tiền hai lần.",
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-1", "topic": topic},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("duplicate_charge", {"get_payment_timeline"}),
        ("refund_pending", {"get_refund_timeline"}),
        ("late_delivery_logistics", {
            "get_order_items", "get_shipment_summary", "get_sellers",
        }),
    ],
)
def test_tool_plan_covers_business_domains(topic: str, expected: set[str]) -> None:
    gateway, case = FakeGateway(), sample_case(topic)
    plan = set(_tool_plan(case, gateway.tools))
    assert {"get_order", "get_order_payments", "get_policy"} <= plan
    assert expected <= plan


def test_shipment_agent_only_activates_for_shipping_topics() -> None:
    assert "shipment-seller-agent" not in _active_specialists(sample_case())
    assert "shipment-seller-agent" in _active_specialists(
        sample_case("late_delivery_seller")
    )


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
        case, gateway, trace, llm=llm,
        models=AgentModels("model", "model", "model", "model", "model"),
    )
    contracts.validate_output(output, "test output")
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert {
        "case_received", "task_assigned", "tool_result_consumed", "handoff",
        "policy_decided", "verification_completed", "case_finalized",
    } <= {event["event_type"] for event in events}
    assert all(call[1] == case["case_id"] for call in gateway.calls)
    assert len(gateway.calls) == len({call[0] for call in gateway.calls})
    assert llm.verifier_calls == (2 if revision_once else 1)
    assert len(llm.calls) == (6 if revision_once else 4)
    assert "shipment_seller_finding" not in llm.calls
    assert sum(event["event_type"] == "verification_completed" for event in events) == 1
    assert output["affected_entities"]["order_ids"] == ["order-1"]
    assert output["financial_resolution"]["recommended_refund_brl"] == sum(
        line["amount_brl"] for line in output["financial_resolution"]["refund_lines"]
    )


def test_shipping_case_runs_three_specialists_concurrently(tmp_path: Path) -> None:
    asyncio.run(_exercise_parallel_workflow(tmp_path))


async def _exercise_parallel_workflow(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway, llm = FakeGateway(delay=0.01), FakeLLM(specialist_delay=0.03)
    await solve_case(sample_case("late_delivery_logistics"), gateway, trace, llm=llm)
    assert len(llm.calls) == 5
    assert llm.max_active_specialists == 3
    assert gateway.max_active > 1


def test_unverified_entities_are_removed_and_audited_evidence_is_retained(tmp_path: Path) -> None:
    asyncio.run(_exercise_evidence_precision(tmp_path))


async def _exercise_evidence_precision(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway, llm, case = FakeGateway(), FakeLLM(), sample_case()
    original = llm.complete_json

    async def selective(**request: Any) -> dict[str, Any]:
        result = await original(**request)
        if request["schema_name"] == "verified_case_decision":
            refs = [
                item["evidence_ref"] for item in request["payload"]["available_evidence"]
            ]
            result["decision"]["evidence_refs"] = refs[:1]
            result["decision"]["claim_assessments"][0]["evidence_refs"] = refs[:1]
            result["decision"]["claim_assessments"][1]["evidence_refs"] = []
            result["decision"]["affected_entities"]["seller_ids"] = ["hallucinated-seller"]
        return result

    llm.complete_json = selective  # type: ignore[method-assign]
    output = await solve_case(case, gateway, trace, llm=llm)
    audited_refs = {
        "ev_" + (tool_name.replace("get_", "") + "_0123456789abcdefghijklmnop")[:32]
        for tool_name, _, _ in gateway.calls
    }
    assert set(output["evidence_refs"]) == audited_refs
    assert output["claim_assessments"][1]["evidence_refs"]
    assert output["claim_assessments"][1]["verdict"] != "insufficient_evidence"
    assert output["affected_entities"]["seller_ids"] == []
