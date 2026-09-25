from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import ContractError, Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ORDER_ID = "e2a03ccf5ea816036608b2d8c3ab8e60"
SELLER_ID = "seller-e2a03ccf5ea8"
ITEM_ID = "item-e2a03ccf5ea8"
CASE_ID = "CASE_001"

TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
}


def evidence_data(
    topic: str = "canceled_order_paid", *, scoped_order_id: str = ORDER_ID
) -> dict[str, Any]:
    order_status = "canceled" if topic == "canceled_order_paid" else "delivered"
    return {
        "get_order": {
            "order_id": scoped_order_id,
            "order_status": order_status,
            "order_delivered_customer_date": None,
        },
        "get_order_items": [
            {
                "order_id": scoped_order_id,
                "order_item_id": ITEM_ID,
                "seller_id": SELLER_ID,
                "price": "79.00",
                "freight_value": "10.00",
                "shipping_limit_date": "2017-12-23T09:00:00-03:00",
            },
            {
                "order_id": scoped_order_id,
                "order_item_id": ITEM_ID,
                "seller_id": SELLER_ID,
                "price": "79.00",
                "freight_value": "18.00",
                "shipping_limit_date": "2018-05-14T09:00:00-03:00",
            },
        ],
        "get_order_payments": [
            {
                "order_id": scoped_order_id,
                "payment_sequential": "1",
                "payment_type": "credit_card",
                "payment_value": "79.00",
            },
            {
                "order_id": scoped_order_id,
                "payment_sequential": "1",
                "payment_type": "credit_card",
                "payment_value": "18.00",
            },
        ],
        "get_payment_timeline": {
            "order_id": scoped_order_id,
            "payments": [],
            "events": [
                {
                    "order_id": scoped_order_id,
                    "event_type": "captured",
                    "status": "confirmed",
                    "amount_brl": "79.00",
                    "event_at": "2017-12-20T10:00:00-03:00",
                }
            ],
        },
        "get_refund_timeline": {
            "order_id": scoped_order_id,
            "events": [
                {
                    "order_id": scoped_order_id,
                    "event_type": "refund_requested",
                    "status": "pending",
                    "amount_brl": "79.00",
                    "event_at": "2017-12-21T10:00:00-03:00",
                }
            ],
        },
        "get_shipment_summary": {
            "order_id": scoped_order_id,
            "delivered_customer_at": None,
            "events": [],
        },
        "get_sellers": [{"seller_id": SELLER_ID}],
        "get_policy": {
            "currency": "BRL",
            "policy_version": "EC_POLICY_V1",
            "rules": {
                "canceled_order_paid": {
                    "case_status": "action_required",
                    "recommended_action": "issue_refund",
                    "refund_brl": 79.0,
                    "responsible_parties": [{"party_type": "platform", "party_id": None}],
                }
            },
        },
    }


class FakeGateway:
    def __init__(self, data: dict[str, Any], tools: set[str] | None = None) -> None:
        self.data = data
        self.tools = tools or set(data)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.refs_by_tool: dict[str, str] = {}

    async def list_tools(self) -> list[str]:
        return sorted(self.tools)

    async def call(self, tool_name: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        index = len(self.calls)
        evidence_ref = f"ev_{index:024d}"
        self.refs_by_tool[tool_name] = evidence_ref
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": evidence_ref,
            "result_hash": "sha256:" + "0" * 64,
            "domain": TOOL_DOMAINS[tool_name],
            "data": self.data[tool_name],
            "warnings": [],
        }


def policy_rule(
    case_status: str,
    action: str,
    refund: float,
    party_type: str = "platform",
    party_id: str | None = None,
) -> dict[str, Any]:
    return {
        "case_status": case_status,
        "recommended_action": action,
        "refund_brl": refund,
        "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
    }


def make_case(topic: str) -> dict[str, Any]:
    return {
        "case_id": CASE_ID,
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "test",
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-001-a", "topic": topic},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


def run_solver(tmp_path: Path, case: dict[str, Any], gateway: FakeGateway) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    contracts.validate_output(output, "test output")
    return output


def test_workflow_builds_closed_schema_output_and_observable_trace(tmp_path: Path) -> None:
    gateway = FakeGateway(evidence_data())
    output = run_solver(tmp_path, make_case("canceled_order_paid"), gateway)

    assert output["assessment"] == {
        "primary_issue": "canceled_order_paid",
        "case_status": "action_required",
        "confidence": 0.88,
    }
    assert output["financial_resolution"] == {
        "currency": "BRL",
        "recommended_refund_brl": 79.0,
        "refund_lines": [
            {
                "reason_code": "issue_refund",
                "amount_brl": 79.0,
                "entity_id": ORDER_ID,
            }
        ],
    }
    assert output["affected_entities"]["seller_ids"] == [SELLER_ID]
    assert set(output["evidence_refs"]) == {f"ev_{index:024d}" for index in range(1, 8)}
    assert gateway.refs_by_tool["get_payment_timeline"] in output["claim_assessments"][0][
        "evidence_refs"
    ]
    assert [conflict["field"] for conflict in output["data_conflicts"]] == [
        "order_items.freight_value",
        "order_items.shipping_limit_date",
        "order_payments.payment_value",
    ]

    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    with pytest.raises(ContractError, match="Additional properties"):
        contracts.validate_output({**output, "unexpected": True}, "invalid output")

    events = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {event["event_type"] for event in events} >= {
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
    }
    event_types = [event["event_type"] for event in events]
    assert event_types.index("tool_result_consumed") < event_types.index("handoff")
    assert event_types.index("handoff") < event_types.index("policy_decided")
    assert event_types.index("policy_decided") < event_types.index("verification_completed")
    assert {event["actor"] for event in events} >= {
        "coordinator",
        "order-agent",
        "payment-agent",
        "shipment-agent",
        "policy-agent",
        "verifier",
    }
    policy_handoff = next(
        event
        for event in events
        if event["event_type"] == "handoff"
        and event["actor"] == "policy-agent"
        and event["target"] == "verifier"
    )
    assert set(policy_handoff["evidence_refs"]) == set(output["evidence_refs"])
    assert {tool for tool, _ in gateway.calls} == {
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_sellers",
        "get_policy",
    }


def test_customer_topic_routes_but_does_not_override_evidence(tmp_path: Path) -> None:
    gateway = FakeGateway(evidence_data())
    output = run_solver(tmp_path, make_case("unsupported_claim"), gateway)

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["confidence"] == 0.69
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_workflow_rejects_cross_scope_evidence(tmp_path: Path) -> None:
    gateway = FakeGateway(evidence_data(scoped_order_id="another-order"))
    with pytest.raises(ContractError, match="cross-scope"):
        run_solver(tmp_path, make_case("canceled_order_paid"), gateway)


def test_malformed_core_timeline_produces_schema_valid_insufficient_output(
    tmp_path: Path,
) -> None:
    data = evidence_data()
    data["get_payment_timeline"]["events"] = None
    output = run_solver(tmp_path, make_case("canceled_order_paid"), FakeGateway(data))

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output["claim_assessments"][0]["verdict"] == "insufficient_evidence"


def test_refund_routing_checks_all_claims_not_only_first(tmp_path: Path) -> None:
    data = evidence_data()
    data["get_order"]["order_status"] = "delivered"
    data["get_policy"]["rules"]["refund_pending"] = policy_rule(
        "needs_investigation", "monitor_refund", 0, "payment_provider"
    )
    case = make_case("unsupported_claim")
    case["customer_request"]["claims"][1]["topic"] = "refund_pending"
    gateway = FakeGateway(data)

    output = run_solver(tmp_path, case, gateway)

    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert "get_refund_timeline" in gateway.refs_by_tool


def test_decimal_normalization_requires_both_split_legs_to_be_captured(
    tmp_path: Path,
) -> None:
    data = evidence_data("valid_split_payment")
    data["get_order_payments"] = [
        {
            "order_id": ORDER_ID,
            "payment_sequential": "1",
            "payment_type": "credit_card",
            "payment_value": "44.5",
        },
        {
            "order_id": ORDER_ID,
            "payment_sequential": "2",
            "payment_type": "voucher",
            "payment_value": "44.50",
        },
    ]
    data["get_payment_timeline"]["events"] = [
        {
            "order_id": ORDER_ID,
            "event_type": "captured",
            "status": "confirmed",
            "amount_brl": "44.5",
            "event_at": "2017-12-20T10:00:00-03:00",
        },
        {
            "order_id": ORDER_ID,
            "event_type": "captured",
            "status": "confirmed",
            "amount_brl": "44.500",
            "event_at": "2017-12-20T11:00:00-03:00",
        },
    ]
    data["get_policy"]["rules"]["valid_split_payment"] = policy_rule(
        "no_action", "document_no_action", 0, "customer"
    )
    data["get_policy"]["rules"]["unsupported_claim"] = policy_rule(
        "no_action", "document_no_action", 0, "customer"
    )

    output = run_solver(tmp_path, make_case("valid_split_payment"), FakeGateway(data))
    assert output["assessment"]["primary_issue"] == "valid_split_payment"

    data["get_payment_timeline"]["events"].pop()
    incomplete_output = run_solver(
        tmp_path / "incomplete",
        make_case("valid_split_payment"),
        FakeGateway(data),
    )
    assert incomplete_output["assessment"]["primary_issue"] == "unsupported_claim"


def test_policy_seller_id_is_preserved_and_used_for_refund(tmp_path: Path) -> None:
    data = evidence_data("late_delivery_seller")
    data["get_order"]["order_delivered_customer_date"] = "2017-12-30T09:00:00-03:00"
    data["get_shipment_summary"] = {
        "order_id": ORDER_ID,
        "delivered_customer_at": "2017-12-30T09:00:00-03:00",
        "events": [
            {
                "order_id": ORDER_ID,
                "event_type": "delivered_late",
                "actor": "seller",
                "status": "confirmed",
                "event_at": "2017-12-30T09:00:00-03:00",
            }
        ],
    }
    data["get_policy"]["rules"]["late_delivery_seller"] = policy_rule(
        "action_required", "refund_freight", 18, "seller", SELLER_ID
    )

    output = run_solver(tmp_path, make_case("late_delivery_seller"), FakeGateway(data))

    assert output["root_cause_analysis"]["responsible_parties"][0]["party_id"] == SELLER_ID
    assert output["financial_resolution"]["refund_lines"][0]["entity_id"] == SELLER_ID


def test_completed_refund_does_not_override_other_classification(tmp_path: Path) -> None:
    data = evidence_data()
    data["get_order"]["order_status"] = "delivered"
    data["get_refund_timeline"]["events"][0]["status"] = "completed"
    data["get_policy"]["rules"]["unsupported_claim"] = policy_rule(
        "no_action", "document_no_action", 0, "customer"
    )

    output = run_solver(tmp_path, make_case("refund_failed"), FakeGateway(data))

    assert output["assessment"]["primary_issue"] == "unsupported_claim"
