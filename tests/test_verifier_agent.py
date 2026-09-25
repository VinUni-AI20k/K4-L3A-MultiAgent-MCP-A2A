from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest

from conftest import ROOT, TOOL_ACTORS, TOOL_DOMAINS, FakeGateway, make_state, read_trace
from student_agent.agents import CaseState, PolicyAgent, VerificationError, VerifierAgent
from student_agent.agents.verifier_agent import (
    PRIMARY_ISSUES,
    RELEVANT_DOMAINS,
    REQUIRED_DOMAINS,
)
from student_agent.trace import TraceWriter

ORDER_ID = "e2a03ccf5ea816036608b2d8c3ab8e60"
EXAMPLE_SELLER = "seller-e58fb7bfd033"  # example id inside the shared policy, not this case's
CASE_SELLER = "seller-0a1b2c3d4e5f"


def collect(
    state: CaseState, gateway: FakeGateway, trace: TraceWriter, *tools: str
) -> dict[str, str]:
    """Run the specialist fetches a real workflow would do, returning tool -> evidence_ref."""

    async def run() -> dict[str, str]:
        refs: dict[str, str] = {}
        await PolicyAgent().load(state, gateway, trace)
        refs["get_policy"] = state.refs_for_domain("policy")[0]
        for tool in tools:
            evidence = await state.fetch(
                gateway, trace, tool_name=tool, actor=TOOL_ACTORS[tool], order_id=ORDER_ID
            )
            refs[tool] = evidence["evidence_ref"]
        return refs

    return asyncio.run(run())


def draft(case_id: str, primary_issue: str, refs: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "assessment": {"primary_issue": primary_issue, "confidence": 0.8},
        "affected_entities": {"order_ids": [ORDER_ID]},
        "evidence_refs": refs,
        **extra,
    }


def test_output_is_schema_valid_and_policy_consistent(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state(topics=("canceled_order_paid", "requested_full_refund"))
    refs = collect(state, gateway, trace, "get_order", "get_order_payments")

    output = VerifierAgent().verify(
        state,
        draft(state.case_id, "canceled_order_paid", [refs["get_order"], refs["get_order"]]),
        trace,
    )

    assert output["assessment"] == {
        "primary_issue": "canceled_order_paid",
        "case_status": "action_required",
        "confidence": 0.8,
    }
    financial = output["financial_resolution"]
    assert financial["recommended_refund_brl"] == 79.0
    assert sum(line["amount_brl"] for line in financial["refund_lines"]) == 79.0
    assert financial["refund_lines"][0]["entity_id"] == ORDER_ID
    assert output["resolution_actions"] == ["issue_refund"]
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "platform", "party_id": None}
    ]
    # Deduplicated, and the required payment + policy evidence was added.
    assert output["evidence_refs"] == [
        refs["get_order"], refs["get_order_payments"], refs["get_policy"]
    ]
    assert output["claim_assessments"][0]["verdict"] == "supported"

    events = read_trace(trace)
    assert [e["event_type"] for e in events[-2:]] == ["policy_decided", "verification_completed"]
    assert events[-1]["decision_code"] == "VERIFIED_WITH_FIXES"


def test_every_cited_ref_is_linked_to_a_consumed_trace_event(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    refs = collect(state, gateway, trace, "get_order", "get_shipment_summary")

    output = VerifierAgent().verify(
        state, draft(state.case_id, "late_delivery_seller", list(refs.values())), trace
    )

    consumed = {
        ref
        for event in read_trace(trace)
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    cited = set(output["evidence_refs"])
    for claim in output.get("claim_assessments", []):
        cited |= set(claim["evidence_refs"])
    assert cited <= consumed


def test_fabricated_cross_case_and_off_topic_refs_are_dropped(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state("L3A_CASE_001")
    refs = collect(state, gateway, trace, "get_order", "get_order_payments", "get_customer_history")
    other = make_state("L3A_CASE_002")
    other_refs = collect(other, gateway, trace, "get_order")

    output = VerifierAgent().verify(
        state,
        draft(
            state.case_id,
            "canceled_order_paid",
            [
                refs["get_order"],
                "ev_fabricated_by_the_model_0001",
                other_refs["get_order"],
                refs["get_customer_history"],
                12345,
            ],
        ),
        trace,
    )

    assert other_refs["get_order"] not in output["evidence_refs"]
    assert "ev_fabricated_by_the_model_0001" not in output["evidence_refs"]
    assert refs["get_customer_history"] not in output["evidence_refs"]
    assert read_trace(trace)[-1]["attributes"]["dropped_refs"] == 4


def test_case_id_mismatch_is_rejected(gateway: FakeGateway, trace: TraceWriter) -> None:
    state = make_state("L3A_CASE_001")
    collect(state, gateway, trace, "get_order")

    with pytest.raises(VerificationError, match="does not match"):
        VerifierAgent().verify(state, draft("L3A_CASE_099", "canceled_order_paid", []), trace)


def test_no_action_case_has_zero_refund_and_records_claim_conflict(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state(topics=("canceled_order_paid",))
    refs = collect(state, gateway, trace, "get_order")

    output = VerifierAgent().verify(
        state,
        draft(
            state.case_id,
            "unsupported_claim",
            [refs["get_order"]],
            financial_resolution={"recommended_refund_brl": 50, "refund_lines": []},
            resolution_actions=["issue_refund", "apologize"],
        ),
        trace,
    )

    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"] == {
        "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []
    }
    assert output["resolution_actions"] == ["document_no_action"]
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    assert output["data_conflicts"] == [
        {
            "field": "assessment.primary_issue",
            "sources": ["customer_claim", "order"],
            "selected_source": "order",
            "resolution_code": "AUTHORITATIVE_EVIDENCE_OVER_CLAIM",
        }
    ]


def test_seller_fault_names_the_case_seller_not_the_policy_example(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    state.seller_ids = [CASE_SELLER]
    refs = collect(state, gateway, trace, "get_order", "get_shipment_summary")

    output = VerifierAgent().verify(
        state, draft(state.case_id, "late_delivery_seller", [refs["get_shipment_summary"]]), trace
    )

    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": CASE_SELLER}
    ]
    assert output["affected_entities"]["seller_ids"] == [CASE_SELLER]
    assert EXAMPLE_SELLER not in json.dumps(output)
    assert output["financial_resolution"]["recommended_refund_brl"] == 18.0


def test_example_seller_never_leaks_when_verifier_decides_itself(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()  # no seller evidence for this case at all
    refs = collect(state, gateway, trace, "get_order", "get_shipment_summary")
    assert state.policy_decision is None  # verify() must call decide() on its own

    output = VerifierAgent().verify(
        state, draft(state.case_id, "late_delivery_seller", [refs["get_shipment_summary"]]), trace
    )

    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": None}
    ]
    assert output["affected_entities"]["seller_ids"] == []
    assert EXAMPLE_SELLER not in json.dumps(output)


def test_unlocalized_policy_decision_from_caller_is_cleaned(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    refs = collect(state, gateway, trace, "get_order", "get_shipment_summary")
    # A caller that skipped localization hands over the raw shared-policy rule.
    state.policy_decision = dataclasses.replace(
        PolicyAgent().lookup(state, "late_delivery_seller"),
        responsible_parties=({"party_type": "seller", "party_id": EXAMPLE_SELLER},),
    )
    base = draft(state.case_id, "late_delivery_seller", [refs["get_shipment_summary"]])
    base["affected_entities"]["seller_ids"] = [CASE_SELLER]

    output = VerifierAgent().verify(state, base, trace)

    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": CASE_SELLER}
    ]
    assert output["affected_entities"]["seller_ids"] == [CASE_SELLER]
    assert EXAMPLE_SELLER not in json.dumps(output)
    assert "unverified_seller_cleared" in read_trace(trace)[-1]["attributes"]["fixes"]


def test_refs_outside_case_state_are_dropped_everywhere(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state(topics=("canceled_order_paid",))
    collect(state, gateway, trace, "get_order", "get_order_payments")
    # Well-formed refs that were never recorded in this CaseState.
    outside = ["ev_never_seen_in_this_case_0001", "ev_never_seen_in_this_case_0002"]

    output = VerifierAgent().verify(
        state,
        draft(
            state.case_id,
            "canceled_order_paid",
            outside,
            claim_assessments=[
                {"claim_id": "claim-001-a", "verdict": "supported", "confidence": 0.9,
                 "evidence_refs": outside},
            ],
        ),
        trace,
    )

    cited = set(output["evidence_refs"])
    for claim in output["claim_assessments"]:
        cited |= set(claim["evidence_refs"])
    assert cited and cited <= set(state.evidence)
    assert not cited & set(outside)
    # Required order + payment + policy evidence is re-added from the case's own records.
    assert {state.evidence[ref].domain for ref in output["evidence_refs"]} == {
        "order", "payment", "policy"
    }
    assert output["claim_assessments"][0]["verdict"] == "insufficient_evidence"
    assert read_trace(trace)[-1]["attributes"]["dropped_refs"] == 2


def test_valid_specialist_refund_lines_are_kept_and_invalid_ones_rebuilt(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    refs = collect(state, gateway, trace, "get_order", "get_order_payments")
    split = [
        {"reason_code": "item_refund", "amount_brl": 70.1, "entity_id": "item-1"},
        {"reason_code": "freight_refund", "amount_brl": 8.9, "entity_id": "item-1"},
    ]
    verifier = VerifierAgent()
    base = draft(state.case_id, "canceled_order_paid", [refs["get_order"]])

    kept = verifier.verify(
        state, {**base, "financial_resolution": {"refund_lines": split}}, trace
    )["financial_resolution"]
    rebuilt = verifier.verify(
        state, {**base, "financial_resolution": {"refund_lines": split[:1]}}, trace
    )["financial_resolution"]

    assert [line["amount_brl"] for line in kept["refund_lines"]] == [70.1, 8.9]
    assert rebuilt["refund_lines"] == [
        {"reason_code": "canceled_order_paid", "amount_brl": 79.0, "entity_id": ORDER_ID}
    ]


def test_missing_required_evidence_lowers_confidence(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    refs = collect(state, gateway, trace, "get_order")  # no payment evidence collected

    output = VerifierAgent().verify(
        state, draft(state.case_id, "canceled_order_paid", [refs["get_order"]]), trace
    )

    assert output["assessment"]["confidence"] == 0.6
    event = read_trace(trace)[-1]
    assert event["decision_code"] == "VERIFIED_MISSING_EVIDENCE"
    assert event["attributes"]["missing_domains"] == "payment"


def test_invalid_issue_and_overconfidence_are_normalized(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    collect(state, gateway, trace, "get_order")
    bad = draft(state.case_id, "customer_is_angry", [])
    bad["assessment"]["confidence"] = 1.0

    output = VerifierAgent().verify(state, bad, trace)

    assert output["assessment"] == {
        "primary_issue": "insufficient_evidence",
        "case_status": "needs_investigation",
        "confidence": 0.6,
    }
    assert output["resolution_actions"] == ["request_additional_evidence"]
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "unknown", "party_id": None}
    ]


def test_claims_without_evidence_or_outside_the_case_are_cleaned(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state(topics=("canceled_order_paid", "requested_full_refund"))
    refs = collect(state, gateway, trace, "get_order", "get_order_payments")
    state.claims_assessed = [
        {"claim_id": "claim-001-b", "verdict": "supported", "confidence": 2, "evidence_refs": []},
        {"claim_id": "claim-999-z", "verdict": "supported", "confidence": 0.9,
         "evidence_refs": [refs["get_order"]]},
    ]

    output = VerifierAgent().verify(
        state, draft(state.case_id, "canceled_order_paid", [refs["get_order"]]), trace
    )

    by_id = {claim["claim_id"]: claim for claim in output["claim_assessments"]}
    assert set(by_id) == {"claim-001-a", "claim-001-b"}
    assert by_id["claim-001-b"]["verdict"] == "insufficient_evidence"
    assert by_id["claim-001-b"]["confidence"] == 0.95


def test_trace_evidence_is_capped_to_contract_limit(
    gateway: FakeGateway, trace: TraceWriter
) -> None:
    state = make_state()
    refs = collect(state, gateway, trace, "get_order_payments", *["get_order"] * 32)

    output = VerifierAgent().verify(
        state, draft(state.case_id, "canceled_order_paid", state.evidence_refs), trace
    )

    assert len(output["evidence_refs"]) == 30
    assert refs["get_policy"] in output["evidence_refs"]
    assert len(read_trace(trace)[-1]["evidence_refs"]) == 20


def test_issue_tables_match_public_output_schema() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "schemas" / "l3a-output-v2.schema.json").read_text(encoding="utf-8")
    )
    assert set(PRIMARY_ISSUES) == set(schema["$defs"]["primaryIssue"]["enum"])
    assert set(REQUIRED_DOMAINS) == set(RELEVANT_DOMAINS) == set(PRIMARY_ISSUES)
    for issue, required in REQUIRED_DOMAINS.items():
        assert set(required) <= RELEVANT_DOMAINS[issue]


def test_every_mcp_tool_has_exactly_one_owning_actor() -> None:
    assert set(TOOL_ACTORS) == set(TOOL_DOMAINS)
    assert len(TOOL_ACTORS) == 10
    assert TOOL_ACTORS["get_policy"] == PolicyAgent.actor
