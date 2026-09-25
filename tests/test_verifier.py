from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.evidence import EvidenceError, EvidenceLedger
from student_agent.trace import TraceWriter
from student_agent.verifier import SpecialistReport, VerifierAgent

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "0123456789abcdef0123456789abcdef"
SELLER = "seller-0123456789ab"
ITEM = "item-0123456789ab"
_counter = itertools.count()

POLICY_RULES = {
    "canceled_order_paid": ("action_required", "issue_refund", 79.0, "platform"),
    "unavailable_order_paid": ("action_required", "issue_refund", 89.0, "seller"),
    "late_delivery_seller": ("action_required", "refund_freight", 18.0, "seller"),
    "late_delivery_logistics": ("action_required", "refund_freight", 16.0, "logistics_provider"),
    "valid_split_payment": ("no_action", "document_no_action", 0.0, "customer"),
    "payment_mismatch": ("action_required", "reconcile_payment", 35.0, "payment_provider"),
    "duplicate_charge": ("action_required", "refund_duplicate_charge", 64.0, "payment_provider"),
    "refund_pending": ("needs_investigation", "monitor_refund", 0.0, "payment_provider"),
    "refund_failed": ("action_required", "retry_refund", 52.0, "payment_provider"),
    "unsupported_claim": ("no_action", "document_no_action", 0.0, "customer"),
}


def ref() -> str:
    return f"ev_test{next(_counter):020d}"


def response(domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": ref(),
        "result_hash": "sha256:" + "0" * 64,
        "domain": domain,
        "data": data,
        "warnings": [],
    }


def policy() -> dict[str, Any]:
    rules = {
        issue: {
            "case_status": status,
            "recommended_action": action,
            "refund_brl": refund,
            "responsible_parties": [
                {"party_type": party, "party_id": "seller-other" if party == "seller" else None}
            ],
        }
        for issue, (status, action, refund, party) in POLICY_RULES.items()
    }
    return {"currency": "BRL", "policy_version": "EC_POLICY_V1", "rules": rules}


def order(status: str = "delivered", delivered: str | None = "2018-03-09T09:00:00-03:00",
          carrier: str = "2018-03-02T09:00:00-03:00") -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "customer_id": "customer-row-0123",
        "order_status": status,
        "order_purchase_timestamp": "2018-02-28T09:00:00-03:00",
        "order_approved_at": "2018-02-28T10:00:00-03:00",
        "order_delivered_carrier_date": carrier,
        "order_delivered_customer_date": delivered,
        "order_estimated_delivery_date": "2018-03-10T09:00:00-03:00",
    }


def item(limit: str = "2018-03-03T09:00:00-03:00", price: str = "79.00",
         freight: str = "10.00") -> dict[str, Any]:
    return {
        "order_id": ORDER_ID, "order_item_id": ITEM, "product_id": "product-0123",
        "seller_id": SELLER, "shipping_limit_date": limit, "price": price,
        "freight_value": freight,
    }


def capture(amount: str, at: str = "2018-02-28T10:00:00-03:00",
            event_type: str = "captured", status: str = "confirmed") -> dict[str, Any]:
    return {"order_id": ORDER_ID, "event_at": at, "event_type": event_type,
            "amount_brl": amount, "status": status}


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.contracts = Contracts(ROOT / "contracts" / "schemas")
        self.trace_path = tmp_path / "trace.jsonl"
        self.ledger = EvidenceLedger()
        self.verifier = VerifierAgent(
            self.contracts, self.ledger, TraceWriter(self.trace_path, self.contracts)
        )

    def run(self, tools: dict[str, tuple[str, Any]], case_id: str = "L3A_CASE_900",
            claim: str = "canceled_order_paid", **report_kwargs: Any) -> dict[str, Any]:
        report = SpecialistReport(agent="test-agent", case_id=case_id, **report_kwargs)
        for tool, (domain, data) in tools.items():
            report.evidence.append(
                self.ledger.record(case_id=case_id, tool_name=tool, actor="test-agent",
                                   response=response(domain, data))
            )
        case = {
            "case_id": case_id,
            "opened_at": "2018-03-12T09:00:00-03:00",
            "customer_request": {
                "claimed_order_id": ORDER_ID,
                "claims": [
                    {"claim_id": "claim-a", "topic": claim},
                    {"claim_id": "claim-b", "topic": "requested_full_refund"},
                ],
            },
            "policy_version": "EC_POLICY_V1",
        }
        output = self.verifier.verify(case, [report])
        self.contracts.validate_output(output, "test output")
        return output

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.trace_path.read_text().splitlines()]


def base_tools(order_row: dict[str, Any], events: list[dict[str, Any]],
               items: list[dict[str, Any]] | None = None,
               shipment_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = items if items is not None else [item()]
    return {
        "get_order": ("order", order_row),
        "get_order_items": ("item", items),
        "get_payment_timeline": ("payment", {"order_id": ORDER_ID, "payments": [],
                                             "events": events}),
        "get_shipment_summary": ("shipment", {
            "order_id": ORDER_ID, "order_status": order_row["order_status"],
            "delivered_carrier_at": order_row["order_delivered_carrier_date"],
            "delivered_customer_at": order_row["order_delivered_customer_date"],
            "estimated_delivery_at": order_row["order_estimated_delivery_date"],
            "shipping_limits": [], "events": shipment_events or [],
        }),
        "get_sellers": ("seller", [{"seller_id": SELLER}]),
        "get_policy": ("policy", policy()),
    }


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_ledger_rejects_cross_case_reuse_and_bad_refs() -> None:
    ledger = EvidenceLedger()
    evidence = response("order", {})
    ledger.record(case_id="CASE_A1", tool_name="get_order", actor="a", response=evidence)
    with pytest.raises(EvidenceError, match="already belongs"):
        ledger.record(case_id="CASE_B1", tool_name="get_order", actor="a", response=evidence)
    with pytest.raises(EvidenceError, match="invalid evidence_ref"):
        ledger.record(case_id="CASE_A1", tool_name="get_order", actor="a",
                      response={**evidence, "evidence_ref": "made-up"})


def test_canceled_order_ignores_out_of_scope_distractors(harness: Harness) -> None:
    events = [capture("79.00"), capture("18.00", at="2018-05-11T10:00:00-03:00")]
    items = [item(), item(limit="2018-05-14T09:00:00-03:00", freight="18.00")]
    output = harness.run(base_tools(order("canceled", delivered=None), events, items))
    assert output["assessment"] == {
        "primary_issue": "canceled_order_paid", "case_status": "action_required",
        "confidence": 0.95,
    }
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["resolution_actions"] == ["issue_refund"]
    verdicts = [c["verdict"] for c in output["claim_assessments"]]
    assert verdicts == ["supported", "supported"]


def test_split_payment_vs_duplicate_charge(harness: Harness) -> None:
    split = harness.run(
        base_tools(order(), [capture("44.50"), capture("44.50", at="2018-02-28T11:00:00-03:00")]),
        case_id="L3A_CASE_901", claim="duplicate_charge",
    )
    assert split["assessment"]["primary_issue"] == "valid_split_payment"
    assert split["assessment"]["case_status"] == "no_action"
    assert split["financial_resolution"] == {
        "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []
    }
    assert split["claim_assessments"][0]["verdict"] == "unsupported"

    duplicate = harness.run(
        base_tools(order(), [capture("64.00"), capture("64.00", at="2018-02-28T11:00:00-03:00")]),
        case_id="L3A_CASE_902", claim="duplicate_charge",
    )
    assert duplicate["assessment"]["primary_issue"] == "duplicate_charge"
    assert duplicate["financial_resolution"]["recommended_refund_brl"] == 64.0


def test_late_delivery_blames_seller_only_on_late_handoff(harness: Harness) -> None:
    late_event = {"event_at": "2018-03-12T09:00:00-03:00", "event_type": "delivered_late",
                  "actor": "seller", "status": "confirmed"}
    seller = harness.run(
        base_tools(order(delivered="2018-03-12T09:00:00-03:00",
                         carrier="2018-03-05T09:00:00-03:00"),
                   [capture("18.00")], items=[item(freight="18.00")],
                   shipment_events=[late_event]),
        case_id="L3A_CASE_903", claim="late_delivery_seller",
    )
    assert seller["assessment"]["primary_issue"] == "late_delivery_seller"
    assert seller["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": SELLER}
    ]
    assert seller["financial_resolution"]["recommended_refund_brl"] == 18.0

    logistics = harness.run(
        base_tools(order(delivered="2018-03-12T09:00:00-03:00"), [capture("16.00")],
                   items=[item(freight="18.00")],
                   shipment_events=[{**late_event, "actor": "logistics_provider"}]),
        case_id="L3A_CASE_904", claim="late_delivery_logistics",
    )
    assert logistics["assessment"]["primary_issue"] == "late_delivery_logistics"
    # Freight refund is capped by what was actually captured.
    assert logistics["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert logistics["assessment"]["confidence"] == 0.95


def test_stale_late_event_does_not_override_on_time_delivery(harness: Harness) -> None:
    stale = {"event_at": "2018-03-05T09:00:00-03:00", "event_type": "delivered_late",
             "actor": "logistics_provider", "status": "confirmed"}
    output = harness.run(
        base_tools(order(), [capture("89.00")], shipment_events=[stale]),
        claim="unsupported_claim",
    )
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_refund_pending_recommends_no_new_refund(harness: Harness) -> None:
    tools = base_tools(order(), [capture("89.00")])
    tools["get_refund_timeline"] = ("refund", {"order_id": ORDER_ID, "events": [
        capture("89.00", at="2018-03-11T09:00:00-03:00", event_type="refund_requested",
                status="pending"),
    ]})
    output = harness.run(tools, claim="refund_pending")
    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output["resolution_actions"] == ["monitor_refund"]


def test_missing_order_evidence_is_insufficient(harness: Harness) -> None:
    output = harness.run({"get_policy": ("policy", policy())})
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["assessment"]["confidence"] <= 0.5


def test_foreign_evidence_is_never_cited_and_trace_is_linked(harness: Harness) -> None:
    foreign = harness.ledger.record(
        case_id="L3A_CASE_999", tool_name="get_order", actor="x",
        response=response("order", order("canceled", delivered=None)),
    )
    output = harness.run(
        base_tools(order("canceled", delivered=None), [capture("79.00")]),
        case_id="L3A_CASE_905",
    )
    report = SpecialistReport(agent="rogue", case_id="L3A_CASE_905", evidence=[foreign])
    assert foreign.evidence_ref not in output["evidence_refs"]
    assert harness.verifier._accept_evidence("L3A_CASE_905", [report]) == ({}, 1)

    events = [e for e in harness.events() if e["case_id"] == "L3A_CASE_905"]
    assert [e["event_type"] for e in events] == [
        "policy_decided", "verification_completed", "handoff"
    ]
    verification = events[1]
    assert verification["evidence_refs"] == output["evidence_refs"]


def test_specialist_disagreement_lowers_confidence_but_evidence_wins(harness: Harness) -> None:
    output = harness.run(
        base_tools(order(), [capture("89.00")]),
        claim="late_delivery_logistics", proposed_issue="late_delivery_logistics",
    )
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["confidence"] == 0.85
