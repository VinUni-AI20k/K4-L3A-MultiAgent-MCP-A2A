"""Synthetic unit fixtures only; these references must never enter a submission."""

from __future__ import annotations

import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from student_agent.analysis import analyze_payment, invoice_total, money, timestamp
from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.evidence import DOMAINS, CaseEvidence
from student_agent.mcp_gateway import ToolFailure
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.verifier import verify_output
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CASE = {
    "case_id": "TEST_CASE_001",
    "opened_at": "2018-01-20T12:00:00-03:00",
    "policy_version": "EC_POLICY_V1",
    "customer_request": {
        "claimed_order_id": "order-test",
        "claims": [
            {"claim_id": "one", "topic": "canceled_order_paid"},
            {"claim_id": "two", "topic": "requested_full_refund"},
        ],
    },
}


def event(kind, amount="100.00", date="2018-01-02T12:00:00-03:00", **extra):
    return {
        "event_type": kind,
        "event_at": date,
        "amount_brl": amount,
        "status": "confirmed",
        "order_id": "order-test",
        **extra,
    }


def data_for(issue):
    order = {"order_id": "order-test", "order_status": "delivered"}
    base = [{"order_id": "order-test", "payment_sequential": "1", "payment_value": "100.00"}]
    captures = [event("captured")]
    shipment = {
        "order_id": "order-test",
        "delivered_carrier_at": "2018-01-03T12:00:00-03:00",
        "delivered_customer_at": "2018-01-09T12:00:00-03:00",
        "estimated_delivery_at": "2018-01-10T12:00:00-03:00",
        "shipping_limits": [
            {
                "order_item_id": "item-test",
                "seller_id": "seller-test",
                "shipping_limit_at": "2018-01-04T12:00:00-03:00",
            }
        ],
        "events": [],
    }
    refund_events = []
    refund = 0
    action, status = "document_no_action", "no_action"
    party = "customer"
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        order["order_status"] = issue.split("_")[0]
        refund, action, status, party = 100, "issue_refund", "action_required", "platform"
    elif issue.startswith("late_delivery"):
        shipment["delivered_customer_at"] = "2018-01-12T12:00:00-03:00"
        if issue == "late_delivery_seller":
            shipment["delivered_carrier_at"] = "2018-01-06T12:00:00-03:00"
        refund, action, status = 10, "refund_freight", "action_required"
        party = "seller" if issue == "late_delivery_seller" else "logistics_provider"
    elif issue == "valid_split_payment":
        base = [
            {"order_id": "order-test", "payment_sequential": "1", "payment_value": "60"},
            {"order_id": "order-test", "payment_sequential": "2", "payment_value": "40"},
        ]
        captures = [event("captured", "60"), event("captured", "40")]
    elif issue == "payment_mismatch":
        captures = [event("captured", "120")]
        refund, action, status, party = (
            20,
            "reconcile_payment",
            "action_required",
            "payment_provider",
        )
    elif issue == "duplicate_charge":
        captures += [event("duplicate_capture")]
        refund, action, status, party = (
            100,
            "refund_duplicate_charge",
            "action_required",
            "payment_provider",
        )
    elif issue.startswith("refund_"):
        refund_events = [event(issue, "50", "2018-01-11T12:00:00-03:00")]
        action, status, party = "monitor_refund", "needs_investigation", "payment_provider"
        if issue == "refund_failed":
            refund, action, status = 50, "retry_refund", "action_required"
    return {
        "get_order": order,
        "get_order_items": [
            {
                "order_id": "order-test",
                "order_item_id": "item-test",
                "seller_id": "seller-test",
                "price": "90",
                "freight_value": "10",
            }
        ],
        "get_order_payments": base,
        "get_payment_timeline": {
            "order_id": "order-test",
            "payments": base,
            "events": captures + refund_events,
        },
        "get_refund_timeline": {"order_id": "order-test", "events": refund_events},
        "get_shipment_summary": shipment,
        "get_sellers": [{"seller_id": "seller-test"}],
        "get_policy": {
            "policy_version": "EC_POLICY_V1",
            "currency": "BRL",
            "rules": {
                issue: {
                    "case_status": status,
                    "recommended_action": action,
                    "refund_brl": refund,
                    "responsible_parties": [
                        {
                            "party_type": party,
                            "party_id": "seller-test" if party == "seller" else None,
                        }
                    ],
                },
            },
        },
    }


class FakeGateway:
    def __init__(self, data):
        self.data, self.calls, self.records = data, [], {}

    async def list_tools(self):
        return list(self.data)

    async def call(self, tool, *, case_id, **arguments):
        self.calls.append((case_id, tool, arguments))
        value = self.data[tool]
        if isinstance(value, Exception):
            raise value
        envelope = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_unit_test_only_" + tool + "x" * 20,
            "result_hash": "sha256:" + "a" * 64,
            "domain": DOMAINS[tool],
            "data": copy.deepcopy(value),
        }
        self.records[tool] = envelope
        return envelope


def execute(tmp_path, data, case=None):
    case = copy.deepcopy(case or CASE)
    contracts = Contracts(ROOT / "contracts/schemas")
    trace = TraceWriter(tmp_path / "traces/trace.jsonl", contracts)
    gateway = FakeGateway(data)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    return output, gateway, trace


@pytest.mark.parametrize(
    "issue",
    [
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
    ],
)
def test_ten_issue_families_and_observable_workflow(tmp_path, issue):
    output, gateway, trace = execute(tmp_path, data_for(issue))
    assert output["assessment"]["primary_issue"] == issue
    trace.contracts.validate_output(output, "unit output")
    assert all(cid == CASE["case_id"] for cid, _, _ in gateway.calls)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / f"{CASE['case_id']}.json").write_text(json.dumps(output))
    cases = CaseSet("test-v1", "l3a", (CASE["case_id"],), {CASE["case_id"]: CASE})
    validate_artifacts(tmp_path, cases, trace.contracts)


def test_claim_topic_does_not_determine_primary_issue(tmp_path):
    case = copy.deepcopy(CASE)
    case["customer_request"]["claims"][0]["topic"] = "duplicate_charge"
    output, _, _ = execute(tmp_path, data_for("canceled_order_paid"), case)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_future_capture_is_excluded_from_as_of_case_total():
    data = data_for("canceled_order_paid")
    data["get_payment_timeline"]["events"].append(
        event("captured", "35", "2018-03-01T12:00:00-03:00")
    )
    facts = analyze_payment(
        data["get_order_payments"], data["get_payment_timeline"], None, timestamp(CASE["opened_at"])
    )
    assert facts["captured"] == Decimal("100.00")
    assert not facts["mismatch"]


def test_latest_refund_completion_supersedes_old_failure():
    data = data_for("refund_failed")
    data["get_refund_timeline"]["events"].append(
        event("refund_completed", "50", "2018-01-12T12:00:00-03:00")
    )
    facts = analyze_payment(
        data["get_order_payments"],
        data["get_payment_timeline"],
        data["get_refund_timeline"],
        timestamp(CASE["opened_at"]),
    )
    assert facts["refund_issue"] is None
    assert facts["refunded"] == Decimal("50.00")


def test_installments_alone_are_not_split_payments(tmp_path):
    data = data_for("unsupported_claim")
    data["get_order_payments"][0]["payment_installments"] = "12"
    output, _, _ = execute(tmp_path, data)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"


def test_duplicate_sequence_rows_are_not_valid_split():
    data = data_for("valid_split_payment")
    data["get_payment_timeline"]["payments"][1]["payment_sequential"] = "1"
    facts = analyze_payment(
        data["get_order_payments"], data["get_payment_timeline"], None, timestamp(CASE["opened_at"])
    )
    assert not facts["split"]
    assert facts["ambiguous_base"]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", None, True])
def test_invalid_money_is_not_zero(bad):
    assert money(bad) is None


def test_missing_policy_rule_does_not_become_no_action(tmp_path):
    data = data_for("canceled_order_paid")
    data["get_policy"]["rules"] = {}
    output, _, _ = execute(tmp_path, data)
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert all(claim["verdict"] == "insufficient_evidence" for claim in output["claim_assessments"])


def test_server_outage_is_fatal_not_a_fabricated_not_found(tmp_path):
    data = data_for("canceled_order_paid")
    data["get_order"] = ToolFailure("service down")
    with pytest.raises(RuntimeError, match="required MCP evidence unavailable"):
        execute(tmp_path, data)


def test_cross_order_evidence_is_rejected(tmp_path):
    data = data_for("canceled_order_paid")
    data["get_order_items"][0]["order_id"] = "another-order"
    with pytest.raises(ValueError, match="cross-order"):
        execute(tmp_path, data)


def test_cancellation_does_not_cite_unrelated_shipment_or_seller(tmp_path):
    output, gateway, _ = execute(tmp_path, data_for("canceled_order_paid"))
    assert gateway.records["get_shipment_summary"]["evidence_ref"] not in output["evidence_refs"]
    assert "get_sellers" not in gateway.records


@pytest.mark.parametrize("corruption", ["foreign_ref", "refund_total", "seller", "entity"])
def test_verifier_rejects_inconsistent_candidates(tmp_path, corruption):
    output, gateway, trace = execute(tmp_path, data_for("canceled_order_paid"))
    ledger = CaseEvidence(
        CASE["case_id"], "order-test", gateway, trace, set(gateway.data), records=gateway.records
    )
    if corruption == "foreign_ref":
        output["evidence_refs"].append("ev_foreign_test_reference_xxxxxxxxxxxx")
    elif corruption == "refund_total":
        output["financial_resolution"]["recommended_refund_brl"] += 1
    elif corruption == "seller":
        output["root_cause_analysis"]["responsible_parties"] = [
            {"party_type": "seller", "party_id": "foreign-seller"}
        ]
    else:
        output["affected_entities"]["item_ids"] = ["invented-item"]
    with pytest.raises(ValueError):
        verify_output(output, ledger)


def test_unresolved_source_conflict_is_not_silently_overridden(tmp_path):
    data = data_for("unsupported_claim")
    data["get_order"]["order_delivered_customer_date"] = "2018-01-12T12:00:00-03:00"
    output, _, _ = execute(tmp_path, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["data_conflicts"][0]["selected_source"] is None


def test_complete_delivery_timestamps_resolve_generic_late_event(tmp_path):
    data = data_for("unsupported_claim")
    data["get_shipment_summary"]["events"] = [
        {
            "order_id": "order-test",
            "event_at": "2018-01-08T12:00:00-03:00",
            "event_type": "delivered_late",
            "actor": "logistics_provider",
            "status": "confirmed",
        }
    ]
    output, _, _ = execute(tmp_path, data)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["confidence"] == 0.9
    assert output["data_conflicts"] == [
        {
            "field": "shipment_delay_attribution",
            "sources": ["shipment_timestamps", "shipment_events"],
            "selected_source": "shipment_timestamps",
            "resolution_code": "USE_COMPLETE_DELIVERY_TIMESTAMPS",
        }
    ]


def test_claims_use_complete_but_precise_evidence_chains(tmp_path):
    case = copy.deepcopy(CASE)
    case["customer_request"]["claims"][0]["topic"] = "late_delivery_seller"
    output, _, _ = execute(tmp_path, data_for("late_delivery_seller"), case)
    primary, refund = output["claim_assessments"]
    assert len(primary["evidence_refs"]) == 5
    assert len(refund["evidence_refs"]) == len(output["evidence_refs"]) == 7


def test_logistics_delay_does_not_cite_seller_profile(tmp_path):
    output, gateway, _ = execute(tmp_path, data_for("late_delivery_logistics"))
    assert "get_sellers" not in gateway.records
    assert gateway.records["get_shipment_summary"]["evidence_ref"] in output["evidence_refs"]


def test_unavailable_order_uses_seller_but_not_shipment_evidence(tmp_path):
    output, gateway, _ = execute(tmp_path, data_for("unavailable_order_paid"))
    assert gateway.records["get_sellers"]["evidence_ref"] in output["evidence_refs"]
    assert gateway.records["get_shipment_summary"]["evidence_ref"] not in output["evidence_refs"]


def test_partial_refund_does_not_support_full_refund(tmp_path):
    output, _, _ = execute(tmp_path, data_for("late_delivery_seller"))
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"


def test_older_capture_does_not_turn_split_into_failed_refund():
    data = data_for("valid_split_payment")
    data["get_payment_timeline"]["payments"].append(
        {"payment_sequential": "1", "payment_value": "52"}
    )
    data["get_payment_timeline"]["events"].append(
        event("captured", "52", "2017-11-01T09:00:00-03:00")
    )
    refunds = {
        "events": [event("refund_requested", "52", "2017-11-12T09:00:00-03:00", status="failed")]
    }
    facts = analyze_payment(
        data["get_order_payments"],
        data["get_payment_timeline"],
        refunds,
        timestamp(CASE["opened_at"]),
        timestamp("2018-01-01T09:00:00-03:00"),
    )
    assert facts["split"]
    assert facts["refund_issue"] is None
    assert facts["captured"] == Decimal("100")


def test_repeated_capture_above_invoice_is_not_valid_split():
    base = [
        {"payment_sequential": "1", "payment_value": "64"},
        {"payment_sequential": "2", "payment_value": "64"},
    ]
    timeline = {
        "payments": base,
        "events": [event("captured", "64"), event("captured", "64", "2018-01-02T13:00:00-03:00")],
    }
    facts = analyze_payment(
        base, timeline, None, timestamp(CASE["opened_at"]), expected_total=Decimal("89")
    )
    assert facts["duplicate"]
    assert not facts["split"]


def test_invoice_ignores_pre_purchase_rows_and_does_not_double_count_item():
    items = [
        {
            "order_item_id": "item",
            "price": "79",
            "freight_value": "10",
            "shipping_limit_date": "2017-11-01T09:00:00-03:00",
        },
        {
            "order_item_id": "item",
            "price": "79",
            "freight_value": "18",
            "shipping_limit_date": "2018-01-03T09:00:00-03:00",
        },
    ]
    assert invoice_total(
        items, timestamp("2018-01-01T09:00:00-03:00"), timestamp(CASE["opened_at"])
    ) == Decimal("97")


def test_reconciliation_mismatch_event_is_authoritative():
    data = data_for("unsupported_claim")
    data["get_payment_timeline"]["events"].append(
        event("reconciliation_mismatch", "35", status="open")
    )
    facts = analyze_payment(
        data["get_order_payments"], data["get_payment_timeline"], None, timestamp(CASE["opened_at"])
    )
    assert facts["mismatch"]


def test_identical_source_records_do_not_create_a_duplicate_charge(tmp_path):
    data = data_for("unavailable_order_paid")
    data["get_payment_timeline"]["events"] *= 2
    data["get_order_payments"] *= 2
    output, _, _ = execute(tmp_path, data)
    assert output["assessment"]["primary_issue"] == "unavailable_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 100
