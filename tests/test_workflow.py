from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.rules import Facts, choose_primary, detect_issues
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "order0000000000000000000000000001"
SELLER_ID = "seller-test"
BUY = "2018-01-10T09:00:00-03:00"
OPENED = "2018-02-01T09:00:00-03:00"
INSIDE = "2018-01-10T10:00:00-03:00"
OUTSIDE = "2018-05-01T10:00:00-03:00"


def item(limit: str, price: str = "79.00", freight: str = "10.00") -> dict[str, str]:
    return {
        "order_id": ORDER_ID,
        "order_item_id": "item-test",
        "seller_id": SELLER_ID,
        "shipping_limit_date": limit,
        "price": price,
        "freight_value": freight,
    }


def event(at: str, event_type: str, amount: str, status: str = "confirmed") -> dict[str, str]:
    return {"event_at": at, "event_type": event_type, "amount_brl": amount, "status": status}


POLICY = {
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_id": None, "party_type": "platform"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-other", "party_type": "seller"}],
        },
        "unsupported_claim": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    }
}


def scenario(**overrides: Any) -> dict[str, Any]:
    order = {
        "order_id": ORDER_ID,
        "order_status": "delivered",
        "order_purchase_timestamp": BUY,
        "order_delivered_carrier_date": "2018-01-12T09:00:00-03:00",
        "order_delivered_customer_date": "2018-01-18T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-01-20T09:00:00-03:00",
    }
    order.update(overrides.pop("order", {}))
    data = {
        "get_order": order,
        "get_order_items": [item("2018-01-13T09:00:00-03:00"), item(OUTSIDE, freight="18.00")],
        "get_payment_timeline": {
            "events": [event(INSIDE, "captured", "89.00"), event(OUTSIDE, "captured", "35.00")]
        },
        "get_refund_timeline": None,
        "get_shipment_summary": {
            "delivered_carrier_at": order["order_delivered_carrier_date"],
            "delivered_customer_at": order["order_delivered_customer_date"],
            "estimated_delivery_at": order["order_estimated_delivery_date"],
            "events": [],
        },
        "get_sellers": [{"seller_id": SELLER_ID}],
        "get_policy": POLICY,
    }
    data.update(overrides)
    return data


class FakeGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append(tool_name)
        if self.data.get(tool_name) is None:
            raise RuntimeError(f"MCP tool {tool_name} failed")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{secrets.token_urlsafe(24)}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": self.data[tool_name],
        }


def run(data: dict[str, Any], topic: str, tmp_path: Path) -> tuple[dict, list[dict], list[str]]:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    case = {
        "case_id": "TEST_CASE_001",
        "opened_at": OPENED,
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-a", "topic": topic},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }
    gateway = FakeGateway(data)
    output = asyncio.run(solve_case(case, gateway, TraceWriter(trace_path, contracts)))
    contracts.validate_output(output, "output")
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    return output, events, gateway.calls


def test_decoy_rows_outside_case_window_are_ignored(tmp_path: Path) -> None:
    output, _, _ = run(scenario(), "unsupported_claim", tmp_path)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert {c["resolution_code"] for c in output["data_conflicts"]} == {
        "EXCLUDED_OUT_OF_CASE_WINDOW"
    }
    assert [c["verdict"] for c in output["claim_assessments"]] == ["unsupported", "unsupported"]


def test_canceled_order_refunds_item_price_capped_by_capture(tmp_path: Path) -> None:
    data = scenario(order={"order_status": "canceled", "order_delivered_customer_date": None})
    output, _, _ = run(data, "canceled_order_paid", tmp_path)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "platform", "party_id": None}
    ]


def test_late_seller_uses_order_seller_not_policy_example(tmp_path: Path) -> None:
    late = {
        "order_delivered_carrier_date": "2018-01-15T09:00:00-03:00",
        "order_delivered_customer_date": "2018-01-25T09:00:00-03:00",
    }
    output, _, calls = run(scenario(order=late), "late_delivery_seller", tmp_path)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["financial_resolution"]["recommended_refund_brl"] == 10.0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": SELLER_ID}
    ]
    assert "get_sellers" in calls


def test_claim_not_backed_by_data_is_rejected(tmp_path: Path) -> None:
    output, _, _ = run(scenario(), "duplicate_charge", tmp_path)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_missing_order_yields_insufficient_evidence(tmp_path: Path) -> None:
    output, events, _ = run(scenario(get_order=None), "canceled_order_paid", tmp_path)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert any(e["event_type"] == "verification_completed" for e in events)


def test_trace_covers_workflow_and_links_cited_evidence(tmp_path: Path) -> None:
    output, events, _ = run(scenario(), "unsupported_claim", tmp_path)
    types = {e["event_type"] for e in events}
    assert {"task_assigned", "handoff", "policy_decided", "verification_completed"} <= types
    consumed = {
        ref
        for e in events
        if e["event_type"] == "tool_result_consumed"
        for ref in e["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    verification = next(e for e in events if e["event_type"] == "verification_completed")
    assert verification["decision_code"] == "PASS"


@pytest.mark.parametrize(
    ("captures", "expected"),
    [
        (["44.50", "44.50"], "valid_split_payment"),
        (["64.00", "64.00"], "duplicate_charge"),
    ],
)
def test_split_versus_duplicate_capture(captures: list[str], expected: str) -> None:
    facts = Facts(order_id=ORDER_ID, order_status="delivered", items=[item(INSIDE)])
    facts.captures = [event(INSIDE, "captured", amount) for amount in captures]
    found = detect_issues(facts)
    assert expected in found
    assert choose_primary(found, expected)[0] == expected


def test_exact_replica_capture_is_not_a_duplicate_charge(tmp_path: Path) -> None:
    replica = event(INSIDE, "captured", "89.00")
    data = scenario(
        order={"order_status": "unavailable", "order_delivered_customer_date": None},
        get_payment_timeline={"events": [replica, dict(replica)]},
    )
    output, _, _ = run(data, "unavailable_order_paid", tmp_path)
    assert output["assessment"]["primary_issue"] == "unavailable_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0


def test_in_window_decoy_item_and_capture_do_not_inflate_refund(tmp_path: Path) -> None:
    late_inside = "2018-01-25T10:00:00-03:00"
    data = scenario(
        order={"order_status": "canceled", "order_delivered_customer_date": None},
        get_order_items=[item("2018-01-13T09:00:00-03:00"), item(late_inside, freight="18.00")],
        get_payment_timeline={
            "events": [event(INSIDE, "captured", "79.00"), event(late_inside, "captured", "18.00")]
        },
    )
    output, _, _ = run(data, "canceled_order_paid", tmp_path)
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["affected_entities"]["item_ids"] == ["item-test"]
