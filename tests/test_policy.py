from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.policy import decide

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "o" * 32
SELLER = "seller-abc123"


def env(tool: str, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{tool}_{'x' * 20}",
        "result_hash": "sha256:" + "0" * 64,
        "domain": domain,
        "data": data,
    }


def case(topic: str) -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_900",
        "opened_at": "2018-03-10T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-a", "topic": topic},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


def evidence(
    status: str = "delivered",
    delivered: str | None = "2018-03-05T09:00:00-03:00",
    carrier: str = "2018-03-02T09:00:00-03:00",
    pay_events: list[dict[str, Any]] | None = None,
    ship_events: list[dict[str, Any]] | None = None,
    refund_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = {
        "get_order": env(
            "get_order",
            "order",
            {
                "order_id": ORDER_ID,
                "order_status": status,
                "order_purchase_timestamp": "2018-03-01T09:00:00-03:00",
                "order_delivered_carrier_date": carrier,
                "order_delivered_customer_date": delivered,
                "order_estimated_delivery_date": "2018-03-08T09:00:00-03:00",
            },
        ),
        "get_order_items": env(
            "get_order_items",
            "item",
            [
                {
                    "order_item_id": "item-1",
                    "seller_id": SELLER,
                    "shipping_limit_date": "2018-03-03T09:00:00-03:00",
                    "price": "79.00",
                    "freight_value": "10.00",
                },
                # distractor row from outside the case window
                {
                    "order_item_id": "item-1",
                    "seller_id": SELLER,
                    "shipping_limit_date": "2018-07-01T09:00:00-03:00",
                    "price": "79.00",
                    "freight_value": "18.00",
                },
            ],
        ),
        "get_shipment_summary": env(
            "get_shipment_summary", "shipment", {"events": ship_events or []}
        ),
        "get_payment_timeline": env(
            "get_payment_timeline",
            "payment",
            {
                "payments": [],
                "events": pay_events
                if pay_events is not None
                else [
                    {
                        "event_at": "2018-03-01T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "89.00",
                        "status": "confirmed",
                    }
                ],
            },
        ),
    }
    if refund_events is not None:
        result["get_refund_timeline"] = env(
            "get_refund_timeline", "refund", {"events": refund_events}
        )
    return result


def captured(at: str, amount: str) -> dict[str, Any]:
    return {"event_at": at, "event_type": "captured", "amount_brl": amount, "status": "confirmed"}


@pytest.fixture(scope="module")
def contracts() -> Contracts:
    return Contracts(ROOT / "contracts" / "schemas")


def check_schema(contracts: Contracts, output: dict[str, Any]) -> None:
    entities = {
        k: [] for k in ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
    }
    contracts.validate_output(
        {
            "schema_version": "day09-l3a-output-v2",
            "case_id": "L3A_CASE_900",
            "affected_entities": entities,
            **output,
        },
        "policy",
    )


def test_canceled_order_paid_refunds_captured_amount(contracts: Contracts) -> None:
    out = decide(case("canceled_order_paid"), evidence(status="canceled", delivered=None))
    check_schema(contracts, out)
    assert out["assessment"]["primary_issue"] == "canceled_order_paid"
    assert out["financial_resolution"]["recommended_refund_brl"] == 89.0
    assert out["resolution_actions"] == ["issue_refund"]


def test_unavailable_order_names_scoped_seller() -> None:
    out = decide(case("unavailable_order_paid"), evidence(status="unavailable", delivered=None))
    assert out["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": SELLER}
    ]


def test_late_delivery_seller_when_handoff_missed() -> None:
    ev = evidence(
        delivered="2018-03-12T09:00:00-03:00",
        carrier="2018-03-06T09:00:00-03:00",
        ship_events=[
            {
                "event_at": "2018-03-12T09:00:00-03:00",
                "event_type": "delivered_late",
                "actor": "seller",
                "status": "confirmed",
            }
        ],
    )
    out = decide(case("late_delivery_seller"), ev)
    assert out["assessment"]["primary_issue"] == "late_delivery_seller"
    assert out["root_cause_analysis"]["responsible_parties"][0]["party_id"] == SELLER


def test_out_of_window_late_event_is_ignored() -> None:
    ev = evidence(
        ship_events=[
            {
                "event_at": "2018-09-01T09:00:00-03:00",
                "event_type": "delivered_late",
                "actor": "logistics_provider",
                "status": "confirmed",
            }
        ]
    )
    out = decide(case("late_delivery_logistics"), ev)
    assert out["assessment"]["primary_issue"] == "unsupported_claim"
    assert out["assessment"]["case_status"] == "no_action"
    assert out["financial_resolution"]["recommended_refund_brl"] == 0


def test_split_payment_matching_total_is_valid() -> None:
    ev = evidence(
        pay_events=[
            captured("2018-03-01T10:00:00-03:00", "44.50"),
            captured("2018-03-01T11:00:00-03:00", "44.50"),
        ]
    )
    assert (
        decide(case("valid_split_payment"), ev)["assessment"]["primary_issue"]
        == "valid_split_payment"
    )


def test_duplicate_capture_over_total() -> None:
    ev = evidence(
        pay_events=[
            captured("2018-03-01T10:00:00-03:00", "64.00"),
            captured("2018-03-01T11:00:00-03:00", "64.00"),
        ]
    )
    out = decide(case("duplicate_charge"), ev)
    assert out["assessment"]["primary_issue"] == "duplicate_charge"
    assert out["financial_resolution"]["recommended_refund_brl"] == 64.0


def test_identical_rows_are_not_a_duplicate_charge() -> None:
    row = captured("2018-03-01T10:00:00-03:00", "89.00")
    out = decide(
        case("canceled_order_paid"),
        evidence(status="canceled", delivered=None, pay_events=[row, dict(row)]),
    )
    assert out["financial_resolution"]["recommended_refund_brl"] == 89.0


def test_refund_pending_needs_investigation_without_refund() -> None:
    ev = evidence(
        refund_events=[
            {
                "event_at": "2018-03-09T09:00:00-03:00",
                "event_type": "refund_requested",
                "amount_brl": "89.00",
                "status": "pending",
            }
        ]
    )
    out = decide(case("refund_pending"), ev)
    assert out["assessment"]["case_status"] == "needs_investigation"
    assert out["financial_resolution"]["recommended_refund_brl"] == 0
    assert out["resolution_actions"] == ["monitor_refund"]


def test_missing_order_is_insufficient_evidence(contracts: Contracts) -> None:
    out = decide(case("duplicate_charge"), {})
    check_schema(contracts, out)
    assert out["assessment"]["primary_issue"] == "insufficient_evidence"
    assert out["evidence_refs"] == []


def test_claim_disagreeing_with_evidence_is_recorded() -> None:
    out = decide(case("duplicate_charge"), evidence())
    assert out["assessment"]["primary_issue"] == "unsupported_claim"
    assert any(c["field"] == "primary_issue" for c in out["data_conflicts"])
    assert out["claim_assessments"][0]["verdict"] == "unsupported"
