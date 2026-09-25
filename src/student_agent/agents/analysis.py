from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def records(value: Any) -> list[dict[str, Any]]:
    """Collect nested record objects without depending on one gateway data shape."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            try:
                marker = json.dumps(node, sort_keys=True, default=str)
            except TypeError:
                marker = repr(node)
            if marker not in seen:
                seen.add(marker)
                found.append(node)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(value)
    return found


def field_values(value: Any, aliases: set[str]) -> list[Any]:
    normalized = {normalize_key(alias) for alias in aliases}
    result: list[Any] = []
    for record in records(value):
        for key, item in record.items():
            if normalize_key(str(key)) in normalized:
                result.append(item)
    return result


def direct_value(record: dict[str, Any], aliases: set[str]) -> Any:
    normalized = {normalize_key(alias) for alias in aliases}
    for key, value in record.items():
        if normalize_key(str(key)) in normalized:
            return value
    return None


def string_values(value: Any, aliases: set[str]) -> list[str]:
    return [
        item.strip()
        for item in field_values(value, aliases)
        if isinstance(item, str) and item.strip()
    ]


def id_values(value: Any, aliases: set[str]) -> list[str]:
    result: list[str] = []
    for item in field_values(value, aliases):
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, list):
            result.extend(
                child.strip() for child in item if isinstance(child, str) and child.strip()
            )
    return result


def unique(values: list[str], limit: int = 20) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
        if len(result) >= limit:
            break
    return result


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
        if match:
            return float(match.group())
    return None


def first_number(value: Any, aliases: set[str]) -> float | None:
    for item in field_values(value, aliases):
        parsed = number(item)
        if parsed is not None and parsed >= 0:
            return parsed
    return None


def sum_numbers(value: Any, aliases: set[str]) -> float:
    total = 0.0
    for item in field_values(value, aliases):
        if isinstance(item, list):
            for child in item:
                parsed = number(child)
                if parsed is not None and parsed >= 0:
                    total += parsed
        else:
            parsed = number(item)
            if parsed is not None and parsed >= 0:
                total += parsed
    return total


def text(value: Any) -> str:
    parts: list[str] = []
    for item in records(value):
        for key, child in item.items():
            if isinstance(child, str):
                parts.append(f"{key} {child}")
    return " ".join(parts).lower()


def has_any(value: Any, terms: tuple[str, ...]) -> bool:
    content = text(value)
    return any(term in content for term in terms)


def parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def first_date(value: Any, aliases: set[str]) -> datetime | None:
    for item in field_values(value, aliases):
        parsed = parse_date(item)
        if parsed is not None:
            return parsed
    return None


def claim_items(context: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for index, raw_claim in enumerate(context.get("claims", []), 1):
        if isinstance(raw_claim, str):
            claim_id = f"claim_{index}"
            claim_text = raw_claim
        elif isinstance(raw_claim, dict):
            raw_id = raw_claim.get("claim_id", raw_claim.get("id"))
            claim_id = raw_id if isinstance(raw_id, str) and raw_id else f"claim_{index}"
            raw_text = raw_claim.get(
                "text",
                raw_claim.get(
                    "description",
                    raw_claim.get("claim", raw_claim.get("topic")),
                ),
            )
            claim_text = raw_text if isinstance(raw_text, str) else json.dumps(raw_claim)
        else:
            claim_id = f"claim_{index}"
            claim_text = str(raw_claim)
        result.append((claim_id[:64], claim_text))
    return result[:5]


def evidence_refs(*sections: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for section in sections:
        for item in section.get("evidence", {}).values():
            reference = item.get("evidence_ref")
            if isinstance(reference, str):
                refs.append(reference)
    return unique(refs, 30)


def data_by_tool(*sections: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section in sections:
        result.update(section.get("data", {}))
    return result


def classify_issue(
    *,
    order: Any,
    shipment: Any,
    payments: Any,
    payment_timeline: Any,
    refunds: Any,
    policy: Any,
    order_total: float | None,
    captured_total: float,
    refunded_total: float,
    seller_delay: bool,
    logistics_delay: bool,
) -> str:
    order_text = text(order)
    payment_text = f"{text(payments)} {text(payment_timeline)}"
    shipment_text = text(shipment)
    paid = (
        captured_total > 0
        or has_any(payment_timeline, ("captured", "approved", "paid", "settled", "charged"))
        or has_any(payments, ("captured", "approved", "paid", "settled", "charged"))
    )
    canceled = has_any(order, ("cancelled", "canceled", "order_cancelled", "order_canceled"))
    unavailable = has_any(order, ("unavailable", "out_of_stock", "out of stock", "not available"))
    refund_failed = has_any(
        refunds,
        ("failed", "rejected", "declined", "error", "refused", "not processed"),
    )
    refund_pending = has_any(
        refunds,
        ("pending", "processing", "requested", "in progress", "awaiting"),
    )
    refund_done = has_any(refunds, ("refunded", "completed", "settled", "paid out"))

    payment_aliases = {
        "payment_value",
        "captured_amount",
        "captured_total",
        "charged_amount",
        "amount_brl",
        "payment_reference",
        "payment_sequential",
    }
    payment_records = [
        record
        for record in records(payments)
        if any(direct_value(record, {key}) is not None for key in payment_aliases)
    ]
    payment_refs = id_values(
        payments,
        {"payment_reference", "payment_ref", "payment_id", "transaction_id", "transaction_ref"},
    )
    duplicate = has_any(
        payments,
        ("duplicate", "duplicated", "double charged", "double capture"),
    ) or (
        order_total is not None
        and captured_total > order_total + 0.01
        and len(payment_records) > 1
    ) or (len(payment_records) > 1 and len(payment_refs) != len(set(payment_refs)))
    mismatch = (
        has_any(payments, ("mismatch", "overcharged", "undercharged", "amount discrepancy"))
        or (
            order_total is not None
            and captured_total > 0
            and abs(captured_total - order_total) > 0.01
        )
    )
    split_payment = (
        len(payment_records) > 1
        and order_total is not None
        and captured_total > 0
        and abs(captured_total - order_total) <= 0.01
    )

    if refund_failed:
        return "refund_failed"
    if refund_pending and not refund_done:
        return "refund_pending"
    if duplicate:
        return "duplicate_charge"
    if canceled and paid:
        return "canceled_order_paid"
    if unavailable and paid:
        return "unavailable_order_paid"
    if seller_delay:
        return "late_delivery_seller"
    if logistics_delay:
        return "late_delivery_logistics"
    if mismatch and not split_payment:
        return "payment_mismatch"
    if split_payment:
        return "valid_split_payment"
    if refund_done and refunded_total <= 0:
        return "refund_failed"
    if not order and not payments and not shipment and not policy:
        return "insufficient_evidence"
    if not payment_text and not shipment_text and not order_text:
        return "insufficient_evidence"
    return "unsupported_claim"


def _late_flags(shipment: Any, items: Any, order: Any) -> tuple[bool, bool]:
    explicit_late = has_any(shipment, ("late", "delayed", "overdue", "delivery delay"))
    seller_delay = explicit_late and has_any(
        shipment,
        ("seller delay", "seller late", "seller_handoff", "late handoff", "dispatch delay"),
    )
    logistics_delay = explicit_late and has_any(
        shipment,
        ("logistics", "carrier", "transport", "in transit", "delivery delay"),
    )
    handoff_at = first_date(
        shipment,
        {
            "seller_handoff_at",
            "seller_handoff_date",
            "shipped_at",
            "dispatch_at",
            "order_delivered_carrier_date",
        },
    )
    if handoff_at is None:
        handoff_at = first_date(order, {"order_delivered_carrier_date"})
    handoff_due = first_date(
        shipment,
        {
            "seller_handoff_due_at",
            "seller_handoff_due_date",
            "seller_dispatch_limit",
            "seller_handoff_limit",
            "shipping_limit_date",
        },
    )
    if handoff_due is None:
        handoff_due = first_date(items, {"shipping_limit_date"})
    delivered_at = first_date(
        shipment,
        {"delivered_at", "delivered_date", "order_delivered_customer_date"},
    )
    if delivered_at is None:
        delivered_at = first_date(order, {"order_delivered_customer_date"})
    estimated_at = first_date(
        shipment,
        {"estimated_delivery_at", "estimated_delivery_date", "order_estimated_delivery_date"},
    )
    if estimated_at is None:
        estimated_at = first_date(order, {"order_estimated_delivery_date"})
    if handoff_at is not None and handoff_due is not None and handoff_at > handoff_due:
        seller_delay = True
    if delivered_at is not None and estimated_at is not None and delivered_at > estimated_at:
        logistics_delay = True
    if explicit_late and not seller_delay and not logistics_delay:
        logistics_delay = True
    return seller_delay, logistics_delay


def _policy_refund(policy: Any, order_total: float | None) -> float:
    amount = first_number(
        policy,
        {
            "refund_amount_brl",
            "compensation_amount_brl",
            "late_delivery_refund_brl",
            "refund_value_brl",
        },
    )
    if amount is not None:
        return amount
    percentage = first_number(
        policy, {"refund_percentage", "refund_percent", "compensation_percent"}
    )
    if percentage is not None and order_total is not None:
        factor = percentage / 100 if percentage > 1 else percentage
        return max(0.0, order_total * factor)
    return 0.0


def _claim_verdict(claim: str, issue: str) -> str:
    lower = claim.lower()
    if issue == "insufficient_evidence":
        return "insufficient_evidence"
    if issue == lower or issue in lower:
        return "supported"
    delivery_claim = any(term in lower for term in ("late", "delivery", "delayed", "shipping"))
    payment_claim = any(
        term in lower for term in ("payment", "charge", "charged", "paid", "transaction")
    )
    refund_claim = any(term in lower for term in ("refund", "reimburse", "money back"))
    if delivery_claim:
        return "supported" if issue.startswith("late_delivery") else "unsupported"
    if payment_claim:
        if issue == "valid_split_payment":
            return "partially_supported"
        return "supported" if issue in {"duplicate_charge", "payment_mismatch"} else "unsupported"
    if refund_claim:
        if issue in {
            "canceled_order_paid",
            "unavailable_order_paid",
            "refund_pending",
            "refund_failed",
        }:
            return "supported"
        if issue in {
            "late_delivery_seller",
            "late_delivery_logistics",
            "duplicate_charge",
            "payment_mismatch",
        }:
            return "partially_supported"
        return "unsupported"
    if issue in {"unsupported_claim", "valid_split_payment"}:
        return "unsupported" if issue == "unsupported_claim" else "partially_supported"
    return "supported"


def build_output(context: dict[str, Any]) -> dict[str, Any]:
    case_id = context["case_info"]["case_id"]
    fulfillment = context.get("fulfillment", {})
    finance = context.get("finance", {})
    all_data = data_by_tool(fulfillment, finance)
    order = all_data.get("get_order", {})
    items = all_data.get("get_order_items", {})
    shipment = all_data.get("get_shipment_summary", {})
    payments = all_data.get("get_order_payments", {})
    payment_timeline = all_data.get("get_payment_timeline", {})
    refunds = all_data.get("get_refund_timeline", {})
    policy = all_data.get("get_policy", {})

    order_id = context.get("claimed_order_id")
    order_ids = unique(
        ([order_id] if isinstance(order_id, str) and order_id else [])
        + id_values(order, {"order_id"})
        + id_values(items, {"order_id"})
        + id_values(payments, {"order_id"})
        + id_values(shipment, {"order_id"})
    )
    item_ids: list[str] = []
    for item in records(items):
        raw_item_id = direct_value(item, {"item_id", "order_item_id"})
        if raw_item_id is None:
            continue
        item_id = str(raw_item_id).strip()
        item_ids.append(f"{order_ids[0]}:{item_id}" if order_ids else item_id)
    item_ids = unique(item_ids)
    seller_ids = unique(
        id_values(items, {"seller_id"}) + id_values(all_data.get("get_sellers", {}), {"seller_id"})
    )
    payment_references = unique(
        id_values(
            payments,
            {"payment_reference", "payment_ref", "payment_id", "transaction_id", "transaction_ref"},
        )
        + id_values(
            payment_timeline,
            {"payment_reference", "payment_ref", "payment_id", "transaction_id", "transaction_ref"},
        )
    )
    if not payment_references and order_ids:
        for payment in records(payments):
            sequential = direct_value(payment, {"payment_sequential"})
            if sequential is not None:
                payment_references.append(f"{order_ids[0]}:{sequential}")
        payment_references = unique(payment_references)
    shipment_ids = unique(
        id_values(shipment, {"shipment_id", "delivery_id", "tracking_id", "tracking_number"})
    )

    item_total = sum_numbers(
        items,
        {"price", "item_price", "item_total_brl", "product_total_brl"},
    )
    freight_total = sum_numbers(
        items,
        {"freight_value", "freight_amount", "freight_brl"},
    )
    order_total = first_number(
        order,
        {
            "order_total_brl",
            "order_total",
            "total_amount",
            "total_brl",
            "total_value",
            "amount_due",
        },
    )
    if order_total is None and item_total + freight_total > 0:
        order_total = item_total + freight_total
    captured_total = sum_numbers(
        payments,
        {"payment_value", "captured_amount", "captured_total", "charged_amount", "amount_brl"},
    )
    refunded_total = sum_numbers(
        refunds,
        {"refund_amount", "refunded_amount", "refunded_total", "amount_refunded", "refund_value"},
    )
    seller_delay, logistics_delay = _late_flags(shipment, items, order)
    issue = classify_issue(
        order=order,
        shipment=shipment,
        payments=payments,
        payment_timeline=payment_timeline,
        refunds=refunds,
        policy=policy,
        order_total=order_total,
        captured_total=captured_total,
        refunded_total=refunded_total,
        seller_delay=seller_delay,
        logistics_delay=logistics_delay,
    )

    refs = evidence_refs(fulfillment, finance)
    error_count = len(fulfillment.get("errors", [])) + len(finance.get("errors", []))
    if issue == "insufficient_evidence":
        confidence = 0.25 if error_count else 0.4
        case_status = "needs_investigation"
    elif issue in {"unsupported_claim", "valid_split_payment"}:
        confidence = 0.8 if refs else 0.45
        case_status = "no_action"
    else:
        confidence = 0.9 if len(refs) >= 3 and not error_count else 0.7
        case_status = "action_required"

    if issue == "late_delivery_seller":
        cause_code = "SELLER_HANDOFF_AFTER_LIMIT"
        responsible = [
            {"party_type": "seller", "party_id": seller_ids[0] if seller_ids else None}
        ]
    elif issue == "late_delivery_logistics":
        cause_code = "CARRIER_DELIVERED_AFTER_ESTIMATE"
        responsible = [{"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}]
    elif issue == "canceled_order_paid":
        cause_code = "ORDER_CANCELED_AFTER_PAYMENT"
        responsible = [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
    elif issue == "unavailable_order_paid":
        cause_code = "ORDER_UNAVAILABLE_AFTER_PAYMENT"
        responsible = [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
    elif issue == "duplicate_charge":
        cause_code = "DUPLICATE_PAYMENT_CAPTURE"
        responsible = [{"party_type": "payment_provider", "party_id": "PAYMENT_PROVIDER"}]
    elif issue == "payment_mismatch":
        cause_code = "PAYMENT_AMOUNT_MISMATCH"
        responsible = [{"party_type": "payment_provider", "party_id": "PAYMENT_PROVIDER"}]
    elif issue == "refund_pending":
        cause_code = "REFUND_PROCESSING_DELAY"
        responsible = [{"party_type": "payment_provider", "party_id": "PAYMENT_PROVIDER"}]
    elif issue == "refund_failed":
        cause_code = "REFUND_FAILURE"
        responsible = [{"party_type": "payment_provider", "party_id": "PAYMENT_PROVIDER"}]
    elif issue == "valid_split_payment":
        cause_code = "MULTIPLE_PAYMENTS_RECONCILED"
        responsible = [{"party_type": "payment_provider", "party_id": None}]
    elif issue == "unsupported_claim":
        cause_code = "CLAIM_NOT_SUPPORTED"
        responsible = [{"party_type": "unknown", "party_id": None}]
    else:
        cause_code = "INSUFFICIENT_EVIDENCE"
        responsible = [{"party_type": "unknown", "party_id": None}]

    recommended_refund = 0.0
    reason_code = issue.upper()
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        recommended_refund = captured_total or (order_total or 0.0)
    elif issue in {"duplicate_charge", "payment_mismatch"} and order_total is not None:
        recommended_refund = max(0.0, captured_total - order_total)
    elif issue == "late_delivery_seller" or issue == "late_delivery_logistics":
        recommended_refund = freight_total or _policy_refund(policy, order_total)
    elif issue in {"refund_pending", "refund_failed"}:
        recommended_refund = max(0.0, captured_total - refunded_total)
    recommended_refund = round(recommended_refund, 2)
    refund_lines = (
        [
            {
                "reason_code": reason_code,
                "amount_brl": recommended_refund,
                "entity_id": order_ids[0] if order_ids else None,
            }
        ]
        if recommended_refund > 0
        else []
    )

    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        actions = ["issue_full_refund", "notify_customer"]
    elif issue in {"late_delivery_seller", "late_delivery_logistics"}:
        actions = ["refund_freight", "notify_customer"]
    elif issue in {"duplicate_charge", "payment_mismatch"}:
        actions = ["correct_payment_record", "refund_excess_charge"]
    elif issue == "refund_pending":
        actions = ["monitor_refund", "notify_customer"]
    elif issue == "refund_failed":
        actions = ["escalate_refund", "notify_customer"]
    elif issue == "valid_split_payment":
        actions = ["explain_valid_split_payment", "close_case"]
    elif issue == "unsupported_claim":
        actions = ["reject_late_refund", "close_case"]
    else:
        actions = ["request_more_evidence", "keep_case_open"]

    claim_assessments = [
        {
            "claim_id": claim_id,
            "verdict": _claim_verdict(claim, issue),
            "confidence": round(confidence, 2),
            "evidence_refs": refs[:10],
        }
        for claim_id, claim in claim_items(context)
    ]

    conflicts: list[dict[str, Any]] = []
    order_statuses = unique(string_values(order, {"order_status", "status"}), 5)
    shipment_order_statuses = unique(string_values(shipment, {"order_status"}), 5)
    if (
        order_statuses
        and shipment_order_statuses
        and set(order_statuses) != set(shipment_order_statuses)
    ):
        conflicts.append(
            {
                "field": "order_status",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_order",
                "resolution_code": "authoritative_order_record",
            }
        )

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": round(confidence, 2),
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
            "responsible_parties": responsible,
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions[:8],
    }
