from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ISSUES = {
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
}

TOOLS_BY_ISSUE: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_order_payments"),
    "unavailable_order_paid": ("get_order", "get_order_items", "get_order_payments"),
    "late_delivery_seller": (
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_sellers",
    ),
    "late_delivery_logistics": ("get_order", "get_shipment_summary"),
    "valid_split_payment": ("get_order_payments", "get_payment_timeline"),
    "payment_mismatch": ("get_order_payments", "get_payment_timeline"),
    "duplicate_charge": ("get_order_payments", "get_payment_timeline"),
    "refund_pending": ("get_order_payments", "get_refund_timeline"),
    "refund_failed": ("get_order_payments", "get_refund_timeline"),
    "unsupported_claim": ("get_order", "get_order_payments", "get_shipment_summary"),
}

TOOL_ACTORS = {
    "get_order": "order-item-agent",
    "get_order_items": "order-item-agent",
    "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
    "get_shipment_summary": "shipment-agent",
    "get_sellers": "shipment-agent",
    "get_policy": "policy-agent",
}

DEFAULT_PARTIES: dict[str, str] = {
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "seller",
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "valid_split_payment": "customer",
    "payment_mismatch": "payment_provider",
    "duplicate_charge": "payment_provider",
    "refund_pending": "payment_provider",
    "refund_failed": "payment_provider",
    "unsupported_claim": "customer",
    "insufficient_evidence": "unknown",
}


def _objects(value: Any) -> Iterable[dict[str, Any]]:
    """Yield every object in an MCP data tree without assuming one response shape."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _positive_amount(data: Any) -> bool:
    amount_keys = {"amount_brl", "payment_value", "value", "refund_brl"}
    for obj in _objects(data):
        for key in amount_keys:
            amount = _decimal(obj.get(key))
            if amount is not None and amount > 0:
                return True
    return False


def _has_record(data: Any, **expected: str) -> bool:
    for obj in _objects(data):
        if all(str(obj.get(key, "")).lower() == value.lower() for key, value in expected.items()):
            return True
    return False


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _order_is_on_time(data: Any) -> bool:
    for obj in _objects(data):
        delivered = _parse_time(obj.get("delivered_customer_at"))
        estimated = _parse_time(obj.get("estimated_delivery_at"))
        if delivered is not None and estimated is not None:
            return delivered <= estimated
    return False


def _distinct_payment_methods(data: Any) -> int:
    methods: set[tuple[str, str]] = set()
    for obj in _objects(data):
        amount = _decimal(obj.get("payment_value", obj.get("amount_brl")))
        payment_type = obj.get("payment_type")
        sequence = obj.get("payment_sequential")
        if amount is not None and amount > 0 and (payment_type is not None or sequence is not None):
            methods.add((str(sequence), str(payment_type)))
    return len(methods)


def _has_duplicate_payment(data: Any) -> bool:
    signatures: list[tuple[str, str, str]] = []
    for obj in _objects(data):
        amount = _decimal(obj.get("payment_value", obj.get("amount_brl")))
        payment_type = obj.get("payment_type")
        sequence = obj.get("payment_sequential")
        if amount is not None and amount > 0 and (payment_type is not None or sequence is not None):
            signatures.append((str(sequence), str(payment_type), str(amount)))
    return any(count > 1 for count in Counter(signatures).values())


def _verify_issue(issue: str, evidence: dict[str, dict[str, Any]]) -> bool:
    data = {name: envelope["data"] for name, envelope in evidence.items()}
    if issue == "canceled_order_paid":
        return _has_record(data["get_order"], order_status="canceled") and _positive_amount(
            data["get_order_payments"]
        )
    if issue == "unavailable_order_paid":
        return _has_record(data["get_order"], order_status="unavailable") and _positive_amount(
            data["get_order_payments"]
        )
    if issue == "late_delivery_seller":
        return _has_record(
            data["get_shipment_summary"],
            event_type="delivered_late",
            actor="seller",
            status="confirmed",
        )
    if issue == "late_delivery_logistics":
        return _has_record(
            data["get_shipment_summary"],
            event_type="delivered_late",
            actor="logistics_provider",
            status="confirmed",
        )
    if issue == "valid_split_payment":
        return _distinct_payment_methods(data["get_order_payments"]) >= 2 and _has_record(
            data["get_payment_timeline"], event_type="captured", status="confirmed"
        )
    if issue == "payment_mismatch":
        return _has_record(data["get_payment_timeline"], event_type="reconciliation_mismatch")
    if issue == "duplicate_charge":
        return _has_duplicate_payment(data["get_order_payments"])
    if issue == "refund_pending":
        return _has_record(data["get_refund_timeline"], status="pending")
    if issue == "refund_failed":
        return _has_record(data["get_refund_timeline"], status="failed")
    if issue == "unsupported_claim":
        return _has_record(data["get_order"], order_status="delivered") and _order_is_on_time(
            data["get_order"]
        )
    return False


def _policy_rule(policy: dict[str, Any], issue: str) -> dict[str, Any] | None:
    data = policy.get("data")
    if not isinstance(data, dict):
        return None
    rules = data.get("rules")
    if not isinstance(rules, dict):
        return None
    rule = rules.get(issue)
    return rule if isinstance(rule, dict) else None


def _money(value: Any) -> float:
    amount = _decimal(value)
    if amount is None or amount < 0:
        return 0.0
    return float(amount.quantize(Decimal("0.01")))


def _parties(rule: dict[str, Any] | None, issue: str) -> list[dict[str, str | None]]:
    raw = rule.get("responsible_parties", []) if rule else []
    result: list[dict[str, str | None]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                party_type = item.get("party_type")
                party_id = item.get("party_id")
            elif isinstance(item, str):
                party_type, party_id = item, None
            else:
                continue
            if party_type in {
                "seller",
                "platform",
                "logistics_provider",
                "payment_provider",
                "customer",
                "unknown",
            }:
                normalized_id = party_id if isinstance(party_id, str) else None
                result.append({"party_type": party_type, "party_id": normalized_id})
    if result:
        return result[:5]
    return [{"party_type": DEFAULT_PARTIES[issue], "party_id": None}]


def _collect_ids(data: Any, keys: set[str]) -> list[str]:
    values: list[str] = []
    for obj in _objects(data):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value and value not in values:
                values.append(value)
    return values[:20]


def _entities(
    order_id: str,
    evidence: dict[str, dict[str, Any]],
    parties: list[dict[str, str | None]],
) -> dict[str, list[str]]:
    data = [envelope["data"] for envelope in evidence.values()]
    seller_ids = _collect_ids(data, {"seller_id"})
    for party in parties:
        if party["party_type"] == "seller" and party["party_id"]:
            party_id = str(party["party_id"])
            if party_id not in seller_ids:
                seller_ids.append(party_id)
    order_ids = _collect_ids(data, {"order_id"})
    if order_id not in order_ids:
        order_ids.insert(0, order_id)
    return {
        "order_ids": order_ids[:20],
        "item_ids": _collect_ids(data, {"item_id", "order_item_id"}),
        "seller_ids": seller_ids[:20],
        "payment_references": _collect_ids(
            data, {"payment_id", "payment_reference", "transaction_id"}
        ),
        "shipment_ids": _collect_ids(data, {"shipment_id", "tracking_id"}),
    }


def _conflicts(issue: str, evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if issue != "unsupported_claim":
        return []
    order = evidence["get_order"]["data"]
    shipment = evidence["get_shipment_summary"]["data"]
    if _order_is_on_time(order) and _has_record(shipment, event_type="delivered_late"):
        return [
            {
                "field": "delivery_timeliness",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_order",
                "resolution_code": "PREFER_ORDER_CUSTOMER_TIMESTAMPS",
            }
        ]
    return []


def _claim_assessments(
    case: dict[str, Any],
    issue: str,
    confirmed: bool,
    refund: float,
    evidence_refs: list[str],
    confidence: float,
) -> list[dict[str, Any]]:
    claims = case.get("customer_request", {}).get("claims", [])
    result: list[dict[str, Any]] = []
    for claim in claims[:5] if isinstance(claims, list) else []:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == issue:
            verdict = "supported" if confirmed else "unsupported"
        elif topic == "requested_full_refund":
            if refund <= 0:
                verdict = "unsupported"
            elif issue in {"canceled_order_paid", "unavailable_order_paid", "refund_failed"}:
                verdict = "supported"
            else:
                verdict = "partially_supported"
        else:
            verdict = "unsupported"
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs,
            }
        )
    return result


async def _consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    order_id: str,
    tool_name: str,
    policy_version: str,
) -> dict[str, Any]:
    arguments = (
        {"policy_version": policy_version}
        if tool_name == "get_policy"
        else {"order_id": order_id}
    )
    evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=TOOL_ACTORS[tool_name],
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
    )
    return evidence


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate scoped specialists and produce a schema-locked L3A decision."""
    case_id = case.get("case_id")
    request = case.get("customer_request")
    policy_version = case.get("policy_version")
    if not isinstance(case_id, str) or not isinstance(request, dict):
        raise ValueError("case must contain case_id and customer_request")
    order_id = request.get("claimed_order_id")
    claims = request.get("claims")
    if not isinstance(order_id, str) or not isinstance(claims, list) or not claims:
        raise ValueError(f"{case_id}: missing claimed order or claims")
    candidate = claims[0].get("topic") if isinstance(claims[0], dict) else None
    if candidate not in ISSUES:
        raise ValueError(f"{case_id}: unsupported primary claim topic {candidate!r}")
    if not isinstance(policy_version, str):
        raise ValueError(f"{case_id}: missing policy_version")

    tools = (*TOOLS_BY_ISSUE[candidate], "get_policy")
    actors = list(dict.fromkeys(TOOL_ACTORS[tool] for tool in tools))
    for actor in actors:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            attributes={"candidate_issue": candidate},
        )

    evidence: dict[str, dict[str, Any]] = {}
    for tool_name in tools:
        evidence[tool_name] = await _consume(
            gateway,
            trace,
            case_id=case_id,
            order_id=order_id,
            tool_name=tool_name,
            policy_version=policy_version,
        )

    for actor in actors:
        actor_refs = [
            envelope["evidence_ref"]
            for tool, envelope in evidence.items()
            if TOOL_ACTORS[tool] == actor
        ]
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="verifier-agent",
            evidence_refs=actor_refs,
        )

    confirmed = _verify_issue(candidate, evidence)
    rule = _policy_rule(evidence["get_policy"], candidate) if confirmed else None
    issue = candidate if confirmed and rule is not None else "insufficient_evidence"
    if issue == "insufficient_evidence":
        case_status = "needs_investigation"
        action = "collect_additional_evidence"
        refund = 0.0
    else:
        raw_status = rule.get("case_status")
        case_status = (
            raw_status
            if raw_status in {"action_required", "no_action", "needs_investigation"}
            else "needs_investigation"
        )
        raw_action = rule.get("recommended_action")
        action = raw_action if isinstance(raw_action, str) and raw_action else "manual_review"
        refund = _money(rule.get("refund_brl"))

    parties = _parties(rule, issue)
    conflicts = _conflicts(candidate, evidence)
    confidence = 0.25 if issue == "insufficient_evidence" else (0.78 if conflicts else 0.95)
    evidence_refs = list(
        dict.fromkeys(envelope["evidence_ref"] for envelope in evidence.values())
    )

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier-agent",
        decision_code=action.upper(),
        evidence_refs=[evidence["get_policy"]["evidence_ref"]],
        attributes={"primary_issue": issue, "refund_brl": refund},
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code="VERIFIED" if confirmed else "INSUFFICIENT_EVIDENCE",
        evidence_refs=evidence_refs,
        attributes={"confidence": confidence, "conflict_count": len(conflicts)},
    )

    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {"reason_code": action.upper(), "amount_brl": refund, "entity_id": order_id}
        )
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": _entities(order_id, evidence, parties),
        "claim_assessments": _claim_assessments(
            case, issue, confirmed, refund, evidence_refs, confidence
        ),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }
