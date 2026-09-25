from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from conftest import FakeGateway, make_state, read_trace
from student_agent.agents import CaseState, PolicyAgent
from student_agent.agents.policy_agent import to_money
from student_agent.trace import TraceWriter


def test_load_fetches_policy_scoped_to_case_and_links_trace(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state("L3A_CASE_007")
    agent = PolicyAgent()

    data = asyncio.run(agent.load(state, gateway, trace))
    asyncio.run(agent.load(state, gateway, trace))  # cached: no second audited call

    assert data["rules"]["canceled_order_paid"]["recommended_action"] == "issue_refund"
    assert gateway.calls == [
        ("get_policy", "L3A_CASE_007", {"policy_version": "EC_POLICY_V1"})
    ]
    [event] = read_trace(trace)
    assert event["event_type"] == "tool_result_consumed"
    assert event["actor"] == "policy-agent"
    assert event["evidence_refs"] == state.refs_for_domain("policy")


def test_decide_applies_policy_rule_and_emits_policy_decided(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    agent = PolicyAgent()
    asyncio.run(agent.load(state, gateway, trace))

    decision = agent.decide(state, "canceled_order_paid", trace)

    assert decision.rule_found
    assert decision.case_status == "action_required"
    assert decision.recommended_action == "issue_refund"
    assert decision.refund_brl == Decimal("79.00")
    assert decision.responsible_parties == ({"party_type": "platform", "party_id": None},)
    assert state.policy_decision is decision
    event = read_trace(trace)[-1]
    assert event["event_type"] == "policy_decided"
    assert event["decision_code"] == "issue_refund"
    assert event["evidence_refs"] == [decision.evidence_ref]


def test_issue_without_rule_falls_back_to_investigation(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    agent = PolicyAgent()
    asyncio.run(agent.load(state, gateway, trace))

    decision = agent.decide(state, "insufficient_evidence", trace)

    assert not decision.rule_found
    assert decision.case_status == "needs_investigation"
    assert decision.recommended_action == "request_additional_evidence"
    assert decision.refund_brl == 0
    assert decision.evidence_ref is None
    assert "evidence_refs" not in read_trace(trace)[-1]


@pytest.mark.parametrize(
    "rule",
    [
        {"case_status": "maybe", "recommended_action": "issue_refund", "refund_brl": 1},
        {"case_status": "action_required", "recommended_action": "", "refund_brl": 1},
        {"case_status": "action_required", "recommended_action": "x", "refund_brl": -5},
        "not-a-rule",
    ],
)
def test_malformed_rule_is_not_trusted(rule: object) -> None:
    state = make_state()
    state.policy_data = {"rules": {"payment_mismatch": rule}}

    decision = PolicyAgent().lookup(state, "payment_mismatch")

    assert not decision.rule_found
    assert decision.recommended_action == "escalate_manual_review"
    assert decision.refund_brl == 0


def test_no_action_rule_never_refunds() -> None:
    state = make_state()
    state.policy_data = {
        "rules": {
            "valid_split_payment": {
                "case_status": "no_action",
                "recommended_action": "document_no_action",
                "refund_brl": 12.5,
                "responsible_parties": [{"party_type": "customer", "party_id": None}],
            }
        }
    }

    assert PolicyAgent().lookup(state, "valid_split_payment").refund_brl == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (79, Decimal("79.00")),
        ("12.345", Decimal("12.35")),
        (0.1 + 0.2, Decimal("0.30")),
        (-1, None),
        (True, None),
        ("abc", None),
        (float("nan"), None),
    ],
)
def test_to_money_rounds_to_cents(raw: object, expected: Decimal | None) -> None:
    assert to_money(raw) == expected


EXAMPLE_SELLER = "seller-e58fb7bfd033"  # example id inside the shared policy


def seller_rule_state(**fields: object) -> CaseState:
    state = make_state()
    state.policy_data = {
        "rules": {
            "late_delivery_seller": {
                "case_status": "action_required",
                "recommended_action": "refund_freight",
                "refund_brl": 18.0,
                "responsible_parties": [
                    {"party_type": "seller", "party_id": EXAMPLE_SELLER},
                    {"party_type": "platform", "party_id": None},
                ],
            }
        }
    }
    for name, value in fields.items():
        setattr(state, name, value)
    return state


def test_shared_policy_seller_is_replaced_by_case_sellers() -> None:
    state = seller_rule_state(seller_ids=["seller-aaa", "seller-bbb", "seller-aaa"])

    decision = PolicyAgent().lookup(state, "late_delivery_seller")

    assert decision.responsible_parties == (
        {"party_type": "seller", "party_id": "seller-aaa"},
        {"party_type": "seller", "party_id": "seller-bbb"},
        {"party_type": "platform", "party_id": None},
    )


def test_shared_policy_seller_becomes_unknown_without_case_evidence() -> None:
    decision = PolicyAgent().lookup(seller_rule_state(), "late_delivery_seller")

    assert decision.responsible_parties[0] == {"party_type": "seller", "party_id": None}
    assert EXAMPLE_SELLER not in str(decision)


def test_case_sellers_come_from_in_window_shipping_limits_only() -> None:
    state = seller_rule_state(
        order_data={"order_purchase_timestamp": "2017-12-20T09:00:00-03:00"},
        shipment_data={
            "shipping_limits": [
                {"seller_id": "seller-real", "shipping_limit_at": "2017-12-23T09:00:00-03:00"},
                # Noise: deadline after the case was opened (2018-01-01).
                {"seller_id": "seller-noise", "shipping_limit_at": "2018-05-14T09:00:00-03:00"},
                # Noise: deadline before the purchase minus one day of slack.
                {"seller_id": "seller-old-noise", "shipping_limit_at": "2017-12-01T09:00:00-03:00"},
                {"seller_id": "", "shipping_limit_at": "2017-12-23T09:00:00-03:00"},
                "not-a-row",
            ]
        },
    )

    assert state.case_seller_ids() == ["seller-real"]
    assert PolicyAgent().lookup(state, "late_delivery_seller").responsible_parties[0] == {
        "party_type": "seller", "party_id": "seller-real"
    }


def test_explicit_case_sellers_take_precedence_over_shipment_data() -> None:
    state = seller_rule_state(
        seller_ids=["seller-from-coordinator"],
        shipment_data={
            "shipping_limits": [
                {"seller_id": "seller-other", "shipping_limit_at": "2017-12-23T09:00:00-03:00"}
            ]
        },
    )

    assert state.case_seller_ids() == ["seller-from-coordinator"]
