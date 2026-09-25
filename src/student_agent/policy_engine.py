"""Deterministic, evidence-backed policy and output verification for L3A."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

PRIMARY_ISSUES = {
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
    "insufficient_evidence",
}

CLAIM_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_order_payments"),
    "unavailable_order_paid": ("get_order", "get_order_items", "get_order_payments"),
    "late_delivery_seller": ("get_order", "get_order_items", "get_shipment_summary"),
    "late_delivery_logistics": ("get_order", "get_shipment_summary"),
    "valid_split_payment": ("get_order_payments", "get_payment_timeline"),
    "payment_mismatch": ("get_order_payments", "get_payment_timeline"),
    "duplicate_charge": ("get_order_payments", "get_payment_timeline"),
    "refund_pending": ("get_order_payments", "get_payment_timeline"),
    "refund_failed": ("get_order_payments", "get_payment_timeline"),
    "unsupported_claim": ("get_order", "get_policy"),
}

CLAIM_PARTIES: dict[str, tuple[str, bool]] = {
    "canceled_order_paid": ("platform", False),
    "unavailable_order_paid": ("seller", True),
    "late_delivery_seller": ("seller", True),
    "late_delivery_logistics": ("logistics_provider", False),
    "payment_mismatch": ("payment_provider", False),
    "duplicate_charge": ("payment_provider", False),
    "refund_pending": ("platform", False),
    "refund_failed": ("payment_provider", False),
}

CLAIM_ACTIONS = {
    "canceled_order_paid": "issue_refund",
    "unavailable_order_paid": "issue_refund",
    "late_delivery_seller": "notify_seller",
    "late_delivery_logistics": "open_logistics_investigation",
    "valid_split_payment": "confirm_split_payment",
    "payment_mismatch": "reconcile_payment",
    "duplicate_charge": "reverse_duplicate_charge",
    "refund_pending": "track_refund",
    "refund_failed": "retry_refund",
    "unsupported_claim": "deny_unsupported_claim",
}


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _strings(payloads: Iterable[dict[str, Any]], *keys: str) -> list[str]:
    values: list[str] = []
    for payload in payloads:
        for record in _walk(payload):
            for key in keys:
                value = record.get(key)
                if isinstance(value, str) and value:
                    values.append(value)
    return list(dict.fromkeys(values))


def _numbers(payloads: Iterable[dict[str, Any]], *keys: str) -> list[float]:
    values: list[float] = []
    for payload in payloads:
        for record in _walk(payload):
            for key in keys:
                value = record.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    values.append(float(value))
                elif isinstance(value, str):
                    try:
                        parsed = float(value.replace(",", "."))
                    except ValueError:
                        continue
                    if parsed >= 0:
                        values.append(parsed)
    return values


def _statuses(payloads: Iterable[dict[str, Any]]) -> set[str]:
    return {
        value.lower()
        for payload in payloads
        for record in _walk(payload)
        for key in ("status", "order_status", "payment_status", "refund_status")
        if isinstance((value := record.get(key)), str)
    }


def _has_true(payloads: Iterable[dict[str, Any]], *keys: str) -> bool:
    return any(record.get(key) is True for payload in payloads for record in _walk(payload) for key in keys)


def _refs(evidence_by_tool: dict[str, list[dict[str, Any]]], tools: Iterable[str]) -> list[str]:
    return [
        str(evidence["evidence_ref"])
        for tool in tools
        for evidence in evidence_by_tool.get(tool, [])
    ]


def _primary_claim(case: dict[str, Any]) -> str | None:
    claims = case.get("customer_request", {}).get("claims", [])
    if not isinstance(claims, list):
        return None
    for claim in claims:
        if isinstance(claim, dict) and claim.get("topic") in CLAIM_REQUIREMENTS:
            return str(claim["topic"])
    return None


def _claim_assessments(case: dict[str, Any], issue: str, refs: list[str]) -> list[dict[str, Any]]:
    claims = case.get("customer_request", {}).get("claims", [])
    if not isinstance(claims, list):
        return []
    result: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        verdict = "supported" if topic == issue and refs else "insufficient_evidence"
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": 0.75 if verdict == "supported" else 0.0,
                "evidence_refs": refs if verdict == "supported" else [],
            }
        )
    return result


def decide(case: dict[str, Any], evidence_by_tool: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Turn validated MCP data into a conservative L3A decision.

    Rules require corroborating order/payment/shipment records. If a required fact
    is absent, the result remains an abstention rather than relying on customer text.
    """
    order = [item["data"] for item in evidence_by_tool.get("get_order", [])]
    items = [item["data"] for item in evidence_by_tool.get("get_order_items", [])]
    payments = [item["data"] for item in evidence_by_tool.get("get_order_payments", [])]
    shipment = [item["data"] for item in evidence_by_tool.get("get_shipment_summary", [])]
    payment_timeline = [item["data"] for item in evidence_by_tool.get("get_payment_timeline", [])]
    refunds = [item["data"] for item in evidence_by_tool.get("get_refund_timeline", [])]

    order_ids = _strings(order, "order_id")
    seller_ids = _strings(items, "seller_id")
    item_ids = _strings(items, "order_item_id", "item_id")
    payment_ids = _strings(
        payments,
        "payment_id",
        "payment_reference",
        "payment_reference_id",
        "transaction_id",
        "charge_id",
    )
    shipment_ids = _strings(shipment, "shipment_id", "tracking_id")
    payment_amounts = _numbers(
        payments,
        "payment_value",
        "captured_amount",
        "captured_total_brl",
        "paid_amount",
        "paid_total_brl",
        "amount_brl",
        "amount",
    )
    if not payment_amounts:
        payment_amounts = _numbers(order, "total_amount", "total_brl", "order_total_brl")
    if not payment_amounts:
        prices = _numbers(items, "price", "item_price", "price_brl")
        freight = _numbers(items, "freight_value", "freight_brl", "shipping_amount")
        payment_amounts = [sum(prices) + sum(freight)] if prices else []
    refund_amounts = _numbers(
        refunds,
        "refund_amount",
        "refund_total_brl",
        "amount_brl",
        "refunded_amount",
        "amount",
    )
    paid_total = sum(payment_amounts)
    refunded_total = sum(refund_amounts)
    statuses = _statuses([*order, *payments, *payment_timeline, *refunds])
    canceled = bool(statuses & {"canceled", "cancelled", "unavailable"})
    refund_statuses = _statuses(refunds)
    failed_refund = bool(refund_statuses & {"refund_failed", "failed"})
    pending_refund = bool(refund_statuses & {"refund_pending", "pending"})
    seller_late = _has_true(shipment, "seller_late", "seller_delay", "late_by_seller")
    logistics_late = _has_true(shipment, "logistics_late", "logistics_delay", "late_by_logistics")
    duplicate = _has_true(payments, "duplicate_charge", "is_duplicate")

    issue = "insufficient_evidence"
    responsible: list[dict[str, str | None]] = []
    action = "collect_additional_evidence"
    relevant_tools: list[str] = []
    refund = 0.0
    if canceled and paid_total > 0 and order and payments:
        issue = "unavailable_order_paid" if "unavailable" in statuses else "canceled_order_paid"
        responsible = [{"party_type": "platform", "party_id": None}]
        refund = max(0.0, paid_total - refunded_total)
        action = "issue_refund" if refund else "confirm_refund_status"
        relevant_tools = ["get_order", "get_order_payments", "get_refund_timeline"]
    elif seller_late and shipment:
        issue = "late_delivery_seller"
        responsible = [{"party_type": "seller", "party_id": seller_ids[0] if seller_ids else None}]
        action = "notify_seller"
        relevant_tools = ["get_order", "get_order_items", "get_shipment_summary"]
    elif logistics_late and shipment:
        issue = "late_delivery_logistics"
        responsible = [{"party_type": "logistics_provider", "party_id": None}]
        action = "open_logistics_investigation"
        relevant_tools = ["get_order", "get_shipment_summary"]
    elif duplicate and payments:
        issue = "duplicate_charge"
        responsible = [{"party_type": "payment_provider", "party_id": None}]
        refund = max(0.0, paid_total - refunded_total)
        action = "reverse_duplicate_charge"
        relevant_tools = ["get_order_payments", "get_payment_timeline"]
    elif failed_refund and payments:
        issue = "refund_failed"
        responsible = [{"party_type": "payment_provider", "party_id": None}]
        refund = max(0.0, paid_total - refunded_total)
        action = "retry_refund"
        relevant_tools = ["get_order_payments", "get_payment_timeline", "get_refund_timeline"]
    elif pending_refund and payments:
        issue = "refund_pending"
        responsible = [{"party_type": "platform", "party_id": None}]
        refund = max(0.0, paid_total - refunded_total)
        action = "track_refund"
        relevant_tools = ["get_order_payments", "get_payment_timeline", "get_refund_timeline"]

    # The customer claim selects the investigation branch, but never substitutes
    # for evidence: every branch has a minimum authoritative evidence set.
    candidate = _primary_claim(case)
    # Payment-timeline payloads may use a generic "pending"/"failed" status
    # for a capture attempt. The case branch determines whether it is a refund
    # state or a payment reconciliation dispute.
    if candidate in {"payment_mismatch", "valid_split_payment"}:
        required = CLAIM_REQUIREMENTS[candidate]
        if all(evidence_by_tool.get(tool) for tool in required):
            issue = candidate
            relevant_tools = list(required)
            action = CLAIM_ACTIONS[candidate]
            responsible = [] if candidate == "valid_split_payment" else [
                {"party_type": "payment_provider", "party_id": None}
            ]
            refund = 0.0
    if issue == "insufficient_evidence" and candidate is not None:
        required = CLAIM_REQUIREMENTS[candidate]
        if all(evidence_by_tool.get(tool) for tool in required):
            issue = candidate
            relevant_tools = list(required)
            action = CLAIM_ACTIONS[candidate]
            if candidate in CLAIM_PARTIES:
                party_type, uses_seller = CLAIM_PARTIES[candidate]
                responsible = [
                    {
                        "party_type": party_type,
                        "party_id": seller_ids[0] if uses_seller and seller_ids else None,
                    }
                ]
            if candidate in {"canceled_order_paid", "unavailable_order_paid", "duplicate_charge"}:
                refund = max(0.0, paid_total - refunded_total)

    refs = _refs(evidence_by_tool, relevant_tools)
    confidence = 0.8 if issue != "insufficient_evidence" and refs else 0.0
    return {
        "issue": issue,
        "confidence": confidence,
        "entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_ids,
            "shipment_ids": shipment_ids,
        },
        "responsible": responsible,
        "refund": refund,
        "action": action,
        "refs": refs,
        "claims": _claim_assessments(case, issue, refs),
    }


def verify(case_id: str, decision: dict[str, Any], schema_version: str) -> dict[str, Any]:
    """Enforce cross-field consistency and confidence bounds before finalization."""
    issue = decision["issue"] if decision["issue"] in PRIMARY_ISSUES else "insufficient_evidence"
    refs = list(dict.fromkeys(decision["refs"]))[:30]
    supported = issue != "insufficient_evidence" and bool(refs)
    no_action = issue in {"valid_split_payment", "unsupported_claim"}
    confidence = min(0.95, max(0.0, float(decision["confidence"]))) if supported else 0.0
    refund = round(max(0.0, float(decision["refund"])), 2) if supported else 0.0
    action = decision["action"] if supported else "collect_additional_evidence"
    responsible = decision["responsible"] if supported else []
    lines = (
        [{"reason_code": issue.upper(), "amount_brl": refund, "entity_id": None}]
        if refund > 0
        else []
    )
    return {
        "schema_version": schema_version,
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": "no_action" if supported and no_action else (
                "action_required" if supported else "needs_investigation"
            ),
            "confidence": confidence,
        },
        "affected_entities": decision["entities"],
        "claim_assessments": decision["claims"],
        "root_cause_analysis": {
            "ranked_causes": ([{"cause_code": issue.upper(), "rank": 1}] if supported else []),
            "responsible_parties": responsible,
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": refund, "refund_lines": lines,
        },
        "resolution_actions": [action],
    }
