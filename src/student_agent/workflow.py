from __future__ import annotations

from decimal import Decimal
from typing import Any

from .a2a import AgentMessage
from .coordinator import investigate_order
from .evidence import EvidenceCollector
from .mcp_gateway import EvidenceGateway
from .observability import record_message
from .policy_agent import inspect_policy
from .state import CaseState
from .trace import TraceWriter

PRIMARY_ISSUES = {
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed", "unsupported_claim",
    "insufficient_evidence",
}


def _facts(state: CaseState) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for message in state.messages:
        for fact in message.facts:
            result[fact.name] = fact.value
    return result


def _evidence_refs(state: CaseState) -> list[str]:
    return list(state.evidence)


def _number(value: Any) -> float:
    return float(Decimal(str(value)))


def _build_output(case: dict[str, Any], state: CaseState) -> dict[str, Any]:
    facts = _facts(state)
    policy = facts.get("policy_rules", {})
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    claims = case["customer_request"]["claims"]
    claim_assessments: list[dict[str, Any]] = []
    selected_rule: dict[str, Any] | None = None
    selected_issue = "insufficient_evidence"

    for claim in claims:
        topic = claim["topic"]
        rule = rules.get(topic) if isinstance(rules.get(topic), dict) else None
        issue = topic if topic in PRIMARY_ISSUES else "unsupported_claim"
        if rule is not None and selected_rule is None and issue in PRIMARY_ISSUES:
            selected_rule = rule
            selected_issue = issue
        claim_assessments.append({
            "claim_id": claim["claim_id"],
            "verdict": "supported" if rule is not None else "insufficient_evidence",
            "confidence": 0.9 if rule is not None else 0.2,
            "evidence_refs": _evidence_refs(state),
        })

    if selected_rule is None:
        selected_rule = {
            "case_status": "needs_investigation",
            "recommended_action": "Collect additional evidence",
            "refund_brl": 0,
            "responsible_parties": [],
        }

    refund = _number(selected_rule.get("refund_brl", 0))
    order_id = state.entity_scope["order_ids"][0]
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": selected_issue,
            "case_status": selected_rule["case_status"],
            "confidence": 0.9 if selected_issue != "insufficient_evidence" else 0.2,
        },
        "affected_entities": {
            "order_ids": [order_id], "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": ([{"cause_code": selected_issue.upper(), "rank": 1}]
                              if selected_issue != "insufficient_evidence" else []),
            "responsible_parties": selected_rule.get("responsible_parties", []),
        },
        "evidence_refs": _evidence_refs(state),
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": ([{
                "reason_code": selected_issue, "amount_brl": refund, "entity_id": order_id,
            }] if refund > 0 else []),
        },
        "resolution_actions": [selected_rule["recommended_action"]],
    }


def _verify_output(output: dict[str, Any], state: CaseState) -> None:
    required = {
        "schema_version", "case_id", "assessment", "affected_entities",
        "root_cause_analysis", "evidence_refs", "data_conflicts",
        "financial_resolution", "resolution_actions", "claim_assessments",
    }
    if set(output) != required:
        raise ValueError("Verifier rejected output fields")
    if output["case_id"] != state.case_id:
        raise ValueError("Verifier rejected mismatched case_id")
    refs = output["evidence_refs"]
    if len(refs) != len(set(refs)) or not set(refs).issubset(state.evidence):
        raise ValueError("Verifier rejected untrusted evidence refs")
    entities = output["affected_entities"]
    if not set(entities["order_ids"]).issubset(state.entity_scope["order_ids"]):
        raise ValueError("Verifier rejected entity outside case scope")
    financial = output["financial_resolution"]
    total = sum(Decimal(str(line["amount_brl"])) for line in financial["refund_lines"])
    if total != Decimal(str(financial["recommended_refund_brl"])):
        raise ValueError("Verifier rejected inconsistent refund total")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the coordinator, specialists, policy agent and verifier."""
    state = await investigate_order(case, gateway, trace)
    collector = EvidenceCollector(gateway, state)

    policy_assignment = AgentMessage(
        case_id=state.case_id, sender="coordinator", recipient="policy-agent",
        task="Apply policy to verified specialist facts",
        entity_scope={"order_ids": list(state.entity_scope["order_ids"])}, status="pending",
    )
    record_message(state, trace, policy_assignment)
    policy_result = await inspect_policy(state, collector, trace)
    record_message(state, trace, policy_result)
    output = _build_output(case, state)
    trace.emit(
        case_id=state.case_id, event_type="policy_decided", actor="policy-agent",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"],
    )

    verifier_assignment = AgentMessage(
        case_id=state.case_id, sender="coordinator", recipient="verifier",
        task="Verify output contract and evidence linkage",
        entity_scope={"order_ids": list(state.entity_scope["order_ids"])},
        status="pending",
    )
    record_message(state, trace, verifier_assignment)
    _verify_output(output, state)
    trace.contracts.validate_output(output, "workflow output")
    trace.emit(
        case_id=state.case_id, event_type="verification_completed", actor="verifier",
        decision_code="PASS", evidence_refs=output["evidence_refs"],
    )
    return output
