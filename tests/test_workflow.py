from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import TOOLS_BY_ISSUE, solve_case

SCENARIOS = {
    "canceled_order_paid": {
        "status": "action_required",
        "action": "issue_refund",
        "refund": 79.0,
        "party": "platform",
    },
    "unavailable_order_paid": {
        "status": "action_required",
        "action": "issue_refund",
        "refund": 89.0,
        "party": "seller",
    },
    "late_delivery_seller": {
        "status": "action_required",
        "action": "refund_freight",
        "refund": 18.0,
        "party": "seller",
    },
    "late_delivery_logistics": {
        "status": "action_required",
        "action": "refund_freight",
        "refund": 16.0,
        "party": "logistics_provider",
    },
    "valid_split_payment": {
        "status": "no_action",
        "action": "document_no_action",
        "refund": 0.0,
        "party": "customer",
    },
    "payment_mismatch": {
        "status": "action_required",
        "action": "reconcile_payment",
        "refund": 35.0,
        "party": "payment_provider",
    },
    "duplicate_charge": {
        "status": "action_required",
        "action": "refund_duplicate_charge",
        "refund": 64.0,
        "party": "payment_provider",
    },
    "refund_pending": {
        "status": "needs_investigation",
        "action": "monitor_refund",
        "refund": 0.0,
        "party": "payment_provider",
    },
    "refund_failed": {
        "status": "action_required",
        "action": "retry_refund",
        "refund": 52.0,
        "party": "payment_provider",
    },
    "unsupported_claim": {
        "status": "no_action",
        "action": "no_action",
        "refund": 0.0,
        "party": "customer",
    },
}


class FakeGateway:
    def __init__(self, issue: str) -> None:
        self.issue = issue
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.ref_by_tool: dict[str, str] = {}

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        number = len(self.calls)
        evidence_ref = f"ev_{tool_name}_{number:020d}"
        self.ref_by_tool[tool_name] = evidence_ref
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": evidence_ref,
            "result_hash": f"sha256:{number:064x}",
            "domain": _domain(tool_name),
            "data": _data(tool_name, self.issue),
        }


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def _domain(tool_name: str) -> str:
    return {
        "get_order": "order",
        "get_order_items": "item",
        "get_order_payments": "payment",
        "get_payment_timeline": "payment",
        "get_refund_timeline": "refund",
        "get_shipment_summary": "shipment",
        "get_sellers": "seller",
        "get_policy": "policy",
    }[tool_name]


def _data(tool_name: str, issue: str) -> Any:
    scenario = SCENARIOS[issue]
    if tool_name == "get_policy":
        return {
            "currency": "BRL",
            "policy_version": "EC_POLICY_V1",
            "rules": {
                issue: {
                    "case_status": scenario["status"],
                    "recommended_action": scenario["action"],
                    "refund_brl": scenario["refund"],
                    "responsible_parties": [
                        {
                            "party_type": scenario["party"],
                            "party_id": (
                                "seller-policy-1" if scenario["party"] == "seller" else None
                            ),
                        }
                    ],
                }
            },
        }
    if tool_name == "get_order":
        status = {
            "canceled_order_paid": "canceled",
            "unavailable_order_paid": "unavailable",
        }.get(issue, "delivered")
        return {
            "order_id": "order-1",
            "order_status": status,
            "timestamps": {
                "delivered_customer_at": "2018-01-07T10:00:00-03:00",
                "estimated_delivery_at": "2018-01-08T10:00:00-03:00",
            },
        }
    if tool_name == "get_order_items":
        return [{"order_id": "order-1", "item_id": "item-1", "seller_id": "seller-1"}]
    if tool_name == "get_order_payments":
        if issue == "duplicate_charge":
            return [
                {"payment_sequential": 1, "payment_type": "credit_card", "payment_value": 64},
                {"payment_sequential": 1, "payment_type": "credit_card", "payment_value": 64},
            ]
        if issue == "valid_split_payment":
            return [
                {"payment_sequential": 1, "payment_type": "credit_card", "payment_value": 44.5},
                {"payment_sequential": 2, "payment_type": "voucher", "payment_value": 44.5},
            ]
        return [{"payment_sequential": 1, "payment_type": "credit_card", "payment_value": 79}]
    if tool_name == "get_payment_timeline":
        if issue == "payment_mismatch":
            return {
                "events": [
                    {
                        "event_type": "reconciliation_mismatch",
                        "amount_brl": 35,
                        "status": "open",
                    }
                ]
            }
        return {"events": [{"event_type": "captured", "status": "confirmed"}]}
    if tool_name == "get_refund_timeline":
        status = "pending" if issue == "refund_pending" else "failed"
        return {
            "events": [
                {"event_type": "refund_requested", "amount_brl": 52, "status": status}
            ]
        }
    if tool_name == "get_shipment_summary":
        actor = "seller" if issue == "late_delivery_seller" else "logistics_provider"
        return {
            "shipment_id": "shipment-1",
            "events": [
                {
                    "event_type": "delivered_late",
                    "actor": actor,
                    "status": "confirmed",
                }
            ],
        }
    if tool_name == "get_sellers":
        return [{"seller_id": "seller-1"}]
    raise AssertionError(tool_name)


def _case(issue: str) -> dict[str, Any]:
    return {
        "case_id": "CASE_001",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-a", "topic": issue},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
    }


@pytest.mark.parametrize("issue", SCENARIOS)
def test_workflow_routes_verifies_and_obeys_policy(issue: str) -> None:
    gateway = FakeGateway(issue)
    trace = FakeTrace()

    output = asyncio.run(solve_case(_case(issue), gateway, trace))  # type: ignore[arg-type]

    scenario = SCENARIOS[issue]
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == issue
    assert output["assessment"]["case_status"] == scenario["status"]
    assert output["financial_resolution"]["recommended_refund_brl"] == scenario["refund"]
    assert output["resolution_actions"] == [scenario["action"]]
    assert output["root_cause_analysis"]["responsible_parties"][0]["party_type"] == scenario[
        "party"
    ]
    assert [call[0] for call in gateway.calls] == [*TOOLS_BY_ISSUE[issue], "get_policy"]
    assert all(call[1] == "CASE_001" for call in gateway.calls)
    assert output["evidence_refs"] == list(gateway.ref_by_tool.values())
    consumed_refs = [
        event["evidence_refs"][0]
        for event in trace.events
        if event["event_type"] == "tool_result_consumed"
    ]
    assert consumed_refs == output["evidence_refs"]
    event_types = [event["event_type"] for event in trace.events]
    assert event_types.index("handoff") < event_types.index("policy_decided")
    assert event_types.index("policy_decided") < event_types.index("verification_completed")


def test_unconfirmed_claim_becomes_insufficient_evidence() -> None:
    gateway = FakeGateway("canceled_order_paid")
    original_call = gateway.call

    async def contradict(tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        evidence = await original_call(tool_name, case_id=case_id, **arguments)
        if tool_name == "get_order":
            evidence["data"]["order_status"] = "delivered"
        return evidence

    gateway.call = contradict  # type: ignore[method-assign]
    output = asyncio.run(
        solve_case(_case("canceled_order_paid"), gateway, FakeTrace())  # type: ignore[arg-type]
    )

    assert output["assessment"] == {
        "primary_issue": "insufficient_evidence",
        "case_status": "needs_investigation",
        "confidence": 0.25,
    }
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "unknown", "party_id": None}
    ]


def test_real_trace_writer_accepts_complete_workflow(tmp_path: Path) -> None:
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    asyncio.run(
        solve_case(_case("payment_mismatch"), FakeGateway("payment_mismatch"), trace)
    )

    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "tool_result_consumed" for event in events)
    assert events[-1]["event_type"] == "verification_completed"
