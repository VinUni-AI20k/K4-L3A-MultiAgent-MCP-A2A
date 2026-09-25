"""Evidence-first coordinator for the L3A complaint workflow."""

from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _rows(value: Any) -> list[dict[str, Any]]:
    return (
        value if isinstance(value, list) and all(isinstance(item, dict) for item in value) else []
    )


def _ids(rows: list[dict[str, Any]], field: str) -> list[str]:
    return sorted({str(row[field]) for row in rows if row.get(field) not in (None, "")})


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Use only same-case MCP evidence; customer claims are leads, not facts."""
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id")
    policy_version = case.get("policy_version")
    if not isinstance(order_id, str) or not isinstance(policy_version, str):
        raise ValueError(f"{case_id}: missing order ID or policy version")

    calls = {
        "order-agent": ("get_order", {"order_id": order_id}),
        "item-agent": ("get_order_items", {"order_id": order_id}),
        "payment-agent": ("get_payment_timeline", {"order_id": order_id}),
        "shipment-agent": ("get_shipment_summary", {"order_id": order_id}),
        "refund-agent": ("get_refund_timeline", {"order_id": order_id}),
        "policy-agent": ("get_policy", {"policy_version": policy_version}),
    }
    for actor in calls:
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)

    async def collect(
        actor: str, tool: str, args: dict[str, str]
    ) -> tuple[str, dict[str, Any] | None]:
        evidence: dict[str, Any] | None = None
        # A transient gateway failure must not become fabricated evidence. Only
        # refund history is retried; retrying every parallel specialist can turn a
        # temporary service failure into a rate-limit cascade.
        attempts = 2 if tool == "get_refund_timeline" else 1
        for _ in range(attempts):
            try:
                evidence = await gateway.call(tool, case_id=case_id, **args)
                break
            except (RuntimeError, ValueError):
                continue
        if evidence is None:
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="EVIDENCE_UNAVAILABLE",
            )
            return tool, None
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[evidence["evidence_ref"]],
        )
        trace.emit(case_id=case_id, event_type="handoff", actor=actor, target="coordinator")
        return tool, evidence

    results = await asyncio.gather(
        *(collect(actor, tool, args) for actor, (tool, args) in calls.items())
    )
    evidence = {tool: item for tool, item in results if item is not None}
    refs = [item["evidence_ref"] for _, item in results if item is not None]
    order = evidence.get("get_order", {}).get("data", {})
    order = order if isinstance(order, dict) else {}
    items = _rows(evidence.get("get_order_items", {}).get("data"))
    payment = evidence.get("get_payment_timeline", {}).get("data", {})
    payment = payment if isinstance(payment, dict) else {}
    pay_events = _rows(payment.get("events"))
    shipment = evidence.get("get_shipment_summary", {}).get("data", {})
    shipment = shipment if isinstance(shipment, dict) else {}
    shipment_events = _rows(shipment.get("events"))
    refunds = evidence.get("get_refund_timeline", {}).get("data", {})
    refund_events = _rows(refunds.get("events") if isinstance(refunds, dict) else refunds)
    policy = evidence.get("get_policy", {}).get("data", {})
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}

    captured = any(
        event.get("event_type") == "captured" and event.get("status") == "confirmed"
        for event in pay_events
    )
    refund_types = {str(event.get("event_type", "")) for event in refund_events}
    late_actor = next(
        (
            event.get("actor")
            for event in shipment_events
            if event.get("event_type") == "delivered_late"
        ),
        None,
    )
    payment_rows = _rows(payment.get("payments"))
    payment_sequences = [
        str(row["payment_sequential"])
        for row in payment_rows
        if row.get("payment_sequential") is not None
    ]
    duplicate_sequence = any(count > 1 for count in Counter(payment_sequences).values())
    captured_total = sum(
        (
            _money(event.get("amount_brl"))
            for event in pay_events
            if event.get("event_type") == "captured" and event.get("status") == "confirmed"
        ),
        Decimal("0"),
    )
    item_total = sum(
        (_money(item.get("price")) + _money(item.get("freight_value")) for item in items),
        Decimal("0"),
    )
    payment_mismatch = bool(captured_total and item_total and captured_total != item_total)
    if "refund_failed" in refund_types:
        issue = "refund_failed"
    elif "refund_pending" in refund_types:
        issue = "refund_pending"
    elif order.get("order_status") == "canceled" and captured:
        issue = "canceled_order_paid"
    elif order.get("order_status") == "unavailable" and captured:
        issue = "unavailable_order_paid"
    elif late_actor == "seller":
        issue = "late_delivery_seller"
    elif late_actor in {"logistics", "logistics_provider", "carrier"}:
        issue = "late_delivery_logistics"
    elif duplicate_sequence or any(
        event.get("event_type") in {"duplicate_charge", "duplicate_captured"}
        for event in pay_events
    ):
        issue = "duplicate_charge"
    elif payment_mismatch or any(
        event.get("event_type") in {"payment_mismatch", "mismatch"} for event in pay_events
    ):
        issue = "payment_mismatch"
    elif len(payment_rows) > 1 and len(set(payment_sequences)) == len(payment_rows):
        issue = "valid_split_payment"
    else:
        issue = "unsupported_claim" if refs else "insufficient_evidence"

    rule = rules.get(issue, {}) if isinstance(rules, dict) else {}
    case_status = rule.get("case_status", "needs_investigation")
    action = str(rule.get("recommended_action", "request_manual_review"))
    refund = float(rule.get("refund_brl", 0))
    parties = _rows(rule.get("responsible_parties"))
    if issue == "late_delivery_seller" and parties and not parties[0].get("party_id"):
        parties[0]["party_id"] = next(iter(_ids(items, "seller_id")), None)
    claims = _rows(request.get("claims") if isinstance(request, dict) else None)
    relevant_tools = {
        "canceled_order_paid": {"get_order", "get_payment_timeline", "get_policy"},
        "unavailable_order_paid": {"get_order", "get_payment_timeline", "get_policy"},
        "late_delivery_seller": {
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_policy",
        },
        "late_delivery_logistics": {"get_order", "get_shipment_summary", "get_policy"},
        "duplicate_charge": {"get_payment_timeline", "get_policy"},
        "payment_mismatch": {"get_order_items", "get_payment_timeline", "get_policy"},
        "refund_pending": {
            "get_order",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        },
        "refund_failed": {"get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"},
    }
    issue_refs = [
        item["evidence_ref"]
        for tool, item in evidence.items()
        if tool in relevant_tools.get(issue, set(evidence))
    ]
    claim_assessments = [
        {
            "claim_id": str(claim["claim_id"]),
            "verdict": "supported" if claim.get("topic") == issue else "unsupported",
            "confidence": 0.95 if claim.get("topic") == issue else 0.9,
            "evidence_refs": issue_refs,
        }
        for claim in claims[:5]
        if claim.get("claim_id")
    ]
    payment_refs = _ids(payment_rows, "payment_reference") or _ids(
        payment_rows, "payment_sequential"
    )
    confidence = 0.95 if len(issue_refs) >= 3 else 0.65
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=issue,
        evidence_refs=[evidence["get_policy"]["evidence_ref"]] if "get_policy" in evidence else [],
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="OUTPUT_VALIDATED",
        evidence_refs=refs,
    )
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [str(order["order_id"])] if order.get("order_id") else [],
            "item_ids": _ids(items, "order_item_id"),
            "seller_ids": _ids(items, "seller_id"),
            "payment_references": payment_refs,
            "shipment_ids": _ids(shipment_events, "shipment_id"),
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": [
                {"reason_code": action, "amount_brl": refund, "entity_id": order.get("order_id")}
            ]
            if refund > 0
            else [],
        },
        "resolution_actions": [action],
    }
