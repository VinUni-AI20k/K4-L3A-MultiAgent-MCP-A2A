from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ToolArguments = dict[str, str]

TOOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "order": ("get_order", "order_get", "lookup_order", "fetch_order"),
    "items": (
        "get_order_items",
        "list_order_items",
        "get_items",
        "get_item",
        "lookup_order_items",
    ),
    "payments": (
        "get_payments",
        "get_order_payments",
        "list_payments",
        "get_payment",
        "lookup_payments",
    ),
    "payment_timeline": ("get_payment_timeline",),
    "shipments": (
        "get_shipment_summary",
        "get_shipments",
        "get_order_shipments",
        "list_shipments",
        "get_shipment",
        "lookup_shipments",
    ),
    "refunds": (
        "get_refund_timeline",
        "get_refunds",
        "get_order_refunds",
        "list_refunds",
        "get_refund",
        "lookup_refunds",
    ),
    "policy": ("get_policy", "get_refund_policy", "lookup_policy", "policy_get"),
    "seller": ("get_sellers", "get_seller", "lookup_seller", "seller_get"),
}

ISSUE_TO_CAUSE = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_DISPATCH_DELAY",
    "late_delivery_logistics": "LOGISTICS_DELIVERY_DELAY",
    "valid_split_payment": "VALID_SPLIT_PAYMENT",
    "payment_mismatch": "PAYMENT_AMOUNT_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PENDING",
    "refund_failed": "REFUND_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}

ACTION_BY_ISSUE = {
    "canceled_order_paid": "issue_full_refund_for_paid_canceled_order",
    "unavailable_order_paid": "issue_full_refund_for_unavailable_order",
    "late_delivery_seller": "refund_shipping_and_escalate_seller_delay",
    "late_delivery_logistics": "refund_shipping_and_open_logistics_claim",
    "payment_mismatch": "reconcile_payment_difference",
    "duplicate_charge": "refund_duplicate_charge",
    "refund_pending": "expedite_pending_refund",
    "refund_failed": "retry_failed_refund",
}

REFUNDABLE_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}


@dataclass(frozen=True)
class Evidence:
    tool_name: str
    evidence_ref: str
    domain: str
    data: Any


def _unique(values: Iterable[Any], *, limit: int = 20) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "payments", "shipments", "refunds", "data", "records", "results"):
            child = value.get(key)
            if isinstance(child, list):
                return child
        return [value]
    return [value]


def _strings_for_keys(data: Any, keys: set[str]) -> list[str]:
    lowered = {key.lower() for key in keys}
    return _unique(value for key, value in _walk(data) if key.lower() in lowered)


def _numbers_for_keys(data: Any, fragments: tuple[str, ...]) -> list[Decimal]:
    result: list[Decimal] = []
    for key, value in _walk(data):
        lowered_key = key.lower()
        if not any(fragment in lowered_key for fragment in fragments):
            continue
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if number >= 0:
            result.append(number)
    return result


def _contains(data: Any, *needles: str) -> bool:
    text = repr(data).lower()
    return any(needle in text for needle in needles)


def _date_for_keys(data: Any, keys: tuple[str, ...]) -> datetime | None:
    wanted = {key.lower() for key in keys}
    for key, value in _walk(data):
        if key.lower() not in wanted or not isinstance(value, str) or not value:
            continue
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            continue
    return None


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _candidate_tool(tools: set[str], domain: str) -> str | None:
    for candidate in TOOL_CANDIDATES[domain]:
        if candidate in tools:
            return candidate
    for tool in sorted(tools):
        lowered = tool.lower()
        if domain.rstrip("s") in lowered and lowered.startswith(("get_", "list_", "lookup_")):
            return tool
    return None


async def _call_evidence(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    tool_name: str | None,
    argument_options: Iterable[ToolArguments],
    required: bool = True,
) -> Evidence | None:
    if tool_name is None:
        if required:
            raise RuntimeError(f"Required MCP tool for {actor} was not discovered")
        return None
    last_error: Exception | None = None
    for arguments in argument_options:
        try:
            response = await gateway.call(tool_name, case_id=case_id, **arguments)
        except (RuntimeError, ValueError) as exc:
            last_error = exc
            continue
        evidence = Evidence(
            tool_name=tool_name,
            evidence_ref=response["evidence_ref"],
            domain=response["domain"],
            data=response["data"],
        )
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence.evidence_ref],
        )
        return evidence
    if required:
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"Required MCP tool {tool_name} failed for {case_id}{detail}")
    return None


def _order_arguments(order_id: str) -> tuple[ToolArguments, ...]:
    return ({"order_id": order_id},)


def _policy_arguments(policy_version: str) -> tuple[ToolArguments, ...]:
    return ({"policy_version": policy_version},)


def _seller_arguments(order_id: str, seller_id: str | None = None) -> tuple[ToolArguments, ...]:
    return ({"order_id": order_id},)


def _extract_issue(case: dict[str, Any], evidence: list[Evidence]) -> tuple[str, float]:
    claims = case.get("customer_request", {}).get("claims", [])
    topics = [claim.get("topic") for claim in claims if isinstance(claim, dict)]
    claimed_issue = next((topic for topic in topics if topic != "requested_full_refund"), None)
    policy_rules = _policy_rules(evidence)
    if isinstance(claimed_issue, str) and claimed_issue in policy_rules:
        return claimed_issue, 0.94

    all_data = [item.data for item in evidence]
    evidence_text = repr(all_data).lower()

    detected: list[tuple[str, float]] = []
    if "canceled" in evidence_text and _contains(all_data, "paid", "approved", "captured"):
        detected.append(("canceled_order_paid", 0.94))
    if "unavailable" in evidence_text and _contains(all_data, "paid", "approved", "captured"):
        detected.append(("unavailable_order_paid", 0.94))
    if _contains(all_data, "duplicate", "duplicated"):
        detected.append(("duplicate_charge", 0.92))
    if _contains(all_data, "mismatch", "divergence", "amount_difference"):
        detected.append(("payment_mismatch", 0.88))
    if _contains(all_data, "refund_pending", "pending_refund") or (
        "pending" in evidence_text and "refund" in evidence_text
    ):
        detected.append(("refund_pending", 0.88))
    if _contains(all_data, "refund_failed", "failed_refund") or (
        "failed" in evidence_text and "refund" in evidence_text
    ):
        detected.append(("refund_failed", 0.88))
    if _contains(all_data, "split") and _contains(all_data, "payment"):
        detected.append(("valid_split_payment", 0.84))

    delivered = _date_for_keys(
        all_data,
        ("order_delivered_customer_date", "delivered_at", "delivery_date", "actual_delivery_at"),
    )
    estimated = _date_for_keys(
        all_data,
        ("order_estimated_delivery_date", "estimated_delivery_at", "promised_delivery_at"),
    )
    carrier = _date_for_keys(
        all_data,
        ("order_delivered_carrier_date", "shipped_at", "carrier_pickup_at", "posted_at"),
    )
    approved = _date_for_keys(
        all_data,
        ("order_approved_at", "approved_at", "payment_approved_at", "purchase_approved_at"),
    )
    if delivered and estimated and delivered > estimated:
        if carrier and estimated and carrier > estimated:
            detected.append(("late_delivery_seller", 0.86))
        elif approved and carrier and (carrier - approved).days >= 7:
            detected.append(("late_delivery_seller", 0.82))
        else:
            detected.append(("late_delivery_logistics", 0.84))

    for issue, confidence in detected:
        if issue == claimed_issue:
            return issue, confidence
    if detected:
        return detected[0]
    if claimed_issue in ISSUE_TO_CAUSE:
        return str(claimed_issue), 0.72
    return "insufficient_evidence", 0.35


def _policy_rules(evidence: list[Evidence]) -> dict[str, Any]:
    for item in evidence:
        if item.domain == "policy" and isinstance(item.data, dict):
            rules = item.data.get("rules")
            if isinstance(rules, dict):
                return rules
    return {}


def _policy_rule(evidence: list[Evidence], issue: str) -> dict[str, Any]:
    rule = _policy_rules(evidence).get(issue)
    return rule if isinstance(rule, dict) else {}


def _policy_refund_amount(evidence: list[Evidence], issue: str) -> Decimal | None:
    rule = _policy_rule(evidence, issue)
    if "refund_brl" not in rule:
        return None
    try:
        return Decimal(str(rule["refund_brl"]))
    except (InvalidOperation, ValueError):
        return None


def _policy_status(evidence: list[Evidence], issue: str) -> str | None:
    status = _policy_rule(evidence, issue).get("case_status")
    if status in {"action_required", "no_action", "needs_investigation"}:
        return str(status)
    return None


def _policy_action(evidence: list[Evidence], issue: str) -> str | None:
    action = _policy_rule(evidence, issue).get("recommended_action")
    return str(action) if isinstance(action, str) and action else None


def _policy_responsible_parties(
    evidence: list[Evidence], issue: str
) -> list[dict[str, str | None]]:
    parties = _policy_rule(evidence, issue).get("responsible_parties")
    if not isinstance(parties, list):
        return []
    result: list[dict[str, str | None]] = []
    for party in parties:
        if not isinstance(party, dict):
            continue
        party_type = party.get("party_type")
        if party_type not in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        }:
            continue
        party_id = party.get("party_id")
        result.append(
            {"party_type": str(party_type), "party_id": str(party_id) if party_id else None}
        )
    return result[:5]


def _case_responsible_parties(
    evidence: list[Evidence], issue: str, seller_ids: list[str]
) -> list[dict[str, str | None]]:
    parties = _policy_responsible_parties(evidence, issue) or _responsible_parties(
        issue, seller_ids
    )
    result: list[dict[str, str | None]] = []
    for party in parties:
        party_type = party["party_type"]
        party_id = party["party_id"]
        if party_type == "seller":
            party_id = seller_ids[0] if seller_ids else party_id
        result.append({"party_type": party_type, "party_id": party_id})
    return result[:5]


def _supporting_evidence_refs(evidence: list[Evidence], issue: str) -> list[str]:
    domains_by_issue = {
        "canceled_order_paid": {"order", "payment", "policy"},
        "unavailable_order_paid": {"order", "item", "seller", "payment", "policy"},
        "late_delivery_seller": {"order", "item", "seller", "shipment", "policy"},
        "late_delivery_logistics": {"order", "item", "shipment", "policy"},
        "valid_split_payment": {"payment", "policy"},
        "payment_mismatch": {"payment", "policy"},
        "duplicate_charge": {"payment", "policy"},
        "refund_pending": {"payment", "refund", "policy"},
        "refund_failed": {"payment", "refund", "policy"},
        "unsupported_claim": {"order", "payment", "shipment", "policy"},
        "insufficient_evidence": {"order", "payment", "shipment", "policy"},
    }
    domains = domains_by_issue.get(issue, {"order", "item", "payment", "shipment", "policy"})
    return _unique((item.evidence_ref for item in evidence if item.domain in domains), limit=30)


def _payment_total(payments: Any) -> Decimal:
    records = _as_list(payments)
    values = _numbers_for_keys(
        records, ("payment_value", "captured_amount", "amount_paid", "total_paid")
    )
    if values:
        return sum(values, Decimal("0"))
    values = _numbers_for_keys(records, ("amount", "value", "total"))
    return max(values, default=Decimal("0"))


def _refund_total(refunds: Any) -> Decimal:
    records = _as_list(refunds)
    values = _numbers_for_keys(records, ("refund_amount", "refunded_amount", "amount_refunded"))
    if values:
        return sum(values, Decimal("0"))
    if _contains(records, "refund"):
        return sum(_numbers_for_keys(records, ("amount", "value", "total")), Decimal("0"))
    return Decimal("0")


def _order_total(order: Any, items: Any, payments: Any) -> Decimal:
    values = _numbers_for_keys(order, ("order_total", "total_amount", "total_value"))
    if values:
        return max(values)
    item_values = _numbers_for_keys(items, ("price", "freight_value", "item_value"))
    if item_values:
        return sum(item_values, Decimal("0"))
    return _payment_total(payments)


def _refund_amount(issue: str, order: Any, items: Any, payments: Any, refunds: Any) -> Decimal:
    paid = _payment_total(payments)
    order_total = _order_total(order, items, payments)
    refunded = _refund_total(refunds)
    full_amount = max(paid, order_total, Decimal("0")) - refunded
    if full_amount < 0:
        full_amount = Decimal("0")

    if issue in REFUNDABLE_ISSUES:
        return full_amount
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        freight = sum(_numbers_for_keys(items, ("freight_value", "shipping_amount")), Decimal("0"))
        return freight if freight > 0 else Decimal("0")
    return Decimal("0")


def _responsible_parties(issue: str, seller_ids: list[str]) -> list[dict[str, str | None]]:
    if issue == "late_delivery_seller":
        return [{"party_type": "seller", "party_id": seller_ids[0] if seller_ids else None}]
    if issue == "late_delivery_logistics":
        return [{"party_type": "logistics_provider", "party_id": None}]
    if issue in {"payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"}:
        return [{"party_type": "payment_provider", "party_id": None}]
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return [{"party_type": "platform", "party_id": None}]
    if issue == "valid_split_payment":
        return [{"party_type": "customer", "party_id": None}]
    return [{"party_type": "unknown", "party_id": None}]


def _status_for(issue: str, refund_amount: Decimal) -> str:
    if issue in {"valid_split_payment", "unsupported_claim"}:
        return "no_action"
    if issue == "insufficient_evidence":
        return "needs_investigation"
    if refund_amount > 0 or issue in ACTION_BY_ISSUE:
        return "action_required"
    return "needs_investigation"


def _claim_assessments(
    case: dict[str, Any],
    issue: str,
    confidence: float,
    evidence_refs: list[str],
    refund_amount: Decimal,
    case_status: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for claim in case.get("customer_request", {}).get("claims", []):
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id", "claim"))
        topic = claim.get("topic")
        if topic == issue:
            verdict = "supported"
            claim_confidence = confidence
        elif topic == "requested_full_refund":
            if refund_amount > 0:
                verdict = "supported"
                claim_confidence = min(confidence, 0.9)
            elif case_status == "needs_investigation":
                verdict = "insufficient_evidence"
                claim_confidence = 0.72
            else:
                verdict = "unsupported"
                claim_confidence = 0.82
        elif issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            claim_confidence = 0.35
        elif issue == "unsupported_claim":
            verdict = "unsupported"
            claim_confidence = 0.78
        else:
            verdict = "partially_supported"
            claim_confidence = min(confidence, 0.7)
        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(claim_confidence, 2),
                "evidence_refs": evidence_refs[:30],
            }
        )
    return result[:5]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate specialist agents and return an L3A contract-compliant output."""
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    order_id = str(request.get("claimed_order_id", "")).strip()
    policy_version = str(case.get("policy_version", "")).strip()
    tools = set(await gateway.list_tools())

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-item-agent",
        decision_code="COLLECT_ORDER_CONTEXT",
        attributes={"tool_count": len(tools)},
    )

    order_tool = _candidate_tool(tools, "order")
    item_tool = _candidate_tool(tools, "items")
    payment_tool = _candidate_tool(tools, "payments")
    payment_timeline_tool = _candidate_tool(tools, "payment_timeline")
    shipment_tool = _candidate_tool(tools, "shipments")
    refund_tool = _candidate_tool(tools, "refunds")
    policy_tool = _candidate_tool(tools, "policy")
    seller_tool = _candidate_tool(tools, "seller")

    evidence: list[Evidence] = []
    order = await _call_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="order-item-agent",
        tool_name=order_tool,
        argument_options=_order_arguments(order_id),
    )
    if order:
        evidence.append(order)
    items = await _call_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="order-item-agent",
        tool_name=item_tool,
        argument_options=_order_arguments(order_id),
    )
    if items:
        evidence.append(items)

    seller_ids = _strings_for_keys([item.data for item in evidence], {"seller_id", "seller"})
    if seller_ids and seller_tool:
        seller = await _call_evidence(
            gateway,
            trace,
            case_id=case_id,
            actor="order-item-agent",
            tool_name=seller_tool,
            argument_options=_seller_arguments(order_id, seller_ids[0]),
            required=False,
        )
        if seller:
            evidence.append(seller)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-item-agent",
        target="payment-agent",
        decision_code="ORDER_CONTEXT_READY",
        evidence_refs=[item.evidence_ref for item in evidence[:5]],
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
        decision_code="RECONCILE_PAYMENT_AND_REFUND",
    )

    payments = await _call_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="payment-agent",
        tool_name=payment_tool,
        argument_options=_order_arguments(order_id),
    )
    if payments:
        evidence.append(payments)
    payment_timeline = await _call_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="payment-agent",
        tool_name=payment_timeline_tool,
        argument_options=_order_arguments(order_id),
        required=False,
    )
    if payment_timeline:
        evidence.append(payment_timeline)
    refunds = await _call_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="payment-agent",
        tool_name=refund_tool,
        argument_options=_order_arguments(order_id),
        required=False,
    )
    if refunds:
        evidence.append(refunds)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment-agent",
        target="shipment-agent",
        decision_code="PAYMENT_CONTEXT_READY",
        evidence_refs=[item.evidence_ref for item in evidence[-5:]],
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
        decision_code="CHECK_TIMELINE_AND_RESPONSIBILITY",
    )

    shipments = await _call_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="shipment-agent",
        tool_name=shipment_tool,
        argument_options=_order_arguments(order_id),
    )
    if shipments:
        evidence.append(shipments)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="policy-agent",
        decision_code="TIMELINE_CONTEXT_READY",
        evidence_refs=[item.evidence_ref for item in evidence[-5:]],
    )
    policy = await _call_evidence(
        gateway,
        trace,
        case_id=case_id,
        actor="policy-agent",
        tool_name=policy_tool,
        argument_options=_policy_arguments(policy_version),
    )
    if policy:
        evidence.append(policy)

    issue, confidence = _extract_issue(case, evidence)

    order_data = order.data if order else {}
    item_data = items.data if items else {}
    payment_data = [source.data for source in (payments, payment_timeline) if source]
    refund_data = refunds.data if refunds else {}
    shipment_data = shipments.data if shipments else {}

    seller_ids = _strings_for_keys(item_data, {"seller_id", "seller"}) or seller_ids
    item_ids = _strings_for_keys(item_data, {"order_item_id", "item_id", "product_id"})
    payment_refs = _strings_for_keys(
        payment_data,
        {"payment_id", "payment_reference", "transaction_id", "payment_sequential"},
    )
    shipment_ids = _strings_for_keys(
        shipment_data,
        {"shipment_id", "tracking_id", "tracking_code", "carrier_id"},
    )
    policy_refund = _policy_refund_amount(evidence, issue)
    refund_amount = (
        policy_refund
        if policy_refund is not None
        else _refund_amount(issue, order_data, item_data, payment_data, refund_data)
    )
    case_status = _policy_status(evidence, issue) or _status_for(issue, refund_amount)
    action = _policy_action(evidence, issue) or ACTION_BY_ISSUE.get(issue)
    resolution_actions = [action] if action else []
    if not resolution_actions:
        resolution_actions = ["collect_additional_evidence"]
    supporting_refs = _supporting_evidence_refs(evidence, issue)
    responsible_parties = _case_responsible_parties(evidence, issue, seller_ids)

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=issue.upper(),
        evidence_refs=supporting_refs[:20],
        attributes={
            "recommended_refund_brl": _money(refund_amount),
            "confidence": round(confidence, 2),
        },
    )

    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": round(confidence if supporting_refs else min(confidence, 0.45), 2),
        },
        "affected_entities": {
            "order_ids": _unique([order_id]),
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": _claim_assessments(
            case, issue, confidence, supporting_refs, refund_amount, case_status
        ),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": ISSUE_TO_CAUSE[issue], "rank": 1}],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": supporting_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _money(refund_amount),
            "refund_lines": [
                {
                    "reason_code": ISSUE_TO_CAUSE[issue],
                    "amount_brl": _money(refund_amount),
                    "entity_id": order_id,
                }
            ]
            if refund_amount > 0
            else [],
        },
        "resolution_actions": _unique(resolution_actions, limit=8),
    }

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="OUTPUT_CONTRACT_READY",
        evidence_refs=supporting_refs[:20],
        attributes={
            "evidence_count": len(supporting_refs),
            "refund_line_count": len(output["financial_resolution"]["refund_lines"]),
        },
    )
    return output
