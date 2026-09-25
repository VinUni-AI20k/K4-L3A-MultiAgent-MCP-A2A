from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2

from .config import Settings
from .llm import OpenRouterClient
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ISSUES = {
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed", "unsupported_claim",
    "insufficient_evidence",
}

ACTORS = {
    "get_order": "order-agent", "get_order_items": "order-agent",
    "get_sellers": "order-agent", "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent", "get_refund_timeline": "payment-agent",
    "get_shipment_summary": "shipment-agent", "get_policy": "policy-agent",
}

SYSTEM_PROMPT = """You are the verifier in an evidence-grounded ecommerce investigation.
Choose the single primary issue supported by authoritative MCP evidence, not by the customer's
claim alone. Never invent IDs, amounts, events, or evidence refs. Return JSON only with keys:
primary_issue, confidence, rationale_code, data_conflicts. primary_issue must be one of:
canceled_order_paid, unavailable_order_paid, late_delivery_seller,
late_delivery_logistics, valid_split_payment, payment_mismatch, duplicate_charge,
refund_pending, refund_failed, unsupported_claim, insufficient_evidence.
confidence is 0..1. rationale_code is UPPER_SNAKE_CASE. data_conflicts is an array of objects
with field, sources (at least two short source names), selected_source or null, resolution_code.
Prefer explicit lifecycle events and order state, then reconciled numeric facts. Policy tells the
remedy after an issue is established; it does not prove that the issue occurred."""


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _walk(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _known_ids(evidence: list[dict[str, Any]], key: str) -> list[str]:
    values = {
        str(value)
        for item in evidence
        for found_key, value in _walk(item.get("data"))
        if found_key == key and isinstance(value, (str, int)) and str(value)
    }
    return sorted(values)[:20]


def _fallback_issue(evidence: dict[str, dict[str, Any]]) -> tuple[str, float]:
    searchable = json.dumps(
        {
            name: item.get("data")
            for name, item in evidence.items()
            if name != "get_policy"
        },
        ensure_ascii=False,
    ).lower()
    for issue in (
        "refund_failed", "refund_pending", "duplicate_charge", "payment_mismatch",
        "late_delivery_seller", "late_delivery_logistics", "unavailable_order_paid",
        "canceled_order_paid", "valid_split_payment", "unsupported_claim",
    ):
        if issue in searchable:
            return issue, 0.9
    if '"order_status": "canceled"' in searchable and '"event_type": "captured"' in searchable:
        return "canceled_order_paid", 0.86
    return "insufficient_evidence", 0.35


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _focused_evidence(evidence: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Remove temporally unrelated rows injected beside the scoped transaction."""
    focused = deepcopy(evidence)
    order = focused.get("get_order", {}).get("data", {})
    purchased = _date(order.get("order_purchase_timestamp")) if isinstance(order, dict) else None
    if purchased is None:
        return focused
    earliest, latest = purchased - timedelta(days=2), purchased + timedelta(days=21)

    def in_scope(row: Any, fields: tuple[str, ...]) -> bool:
        if not isinstance(row, dict):
            return False
        stamp = next((_date(row.get(field)) for field in fields if row.get(field)), None)
        return stamp is not None and earliest <= stamp <= latest

    items = focused.get("get_order_items", {}).get("data")
    if isinstance(items, list):
        focused["get_order_items"]["data"] = [
            row
            for row in items
            if (
                (stamp := _date(row.get("shipping_limit_date"))) is not None
                and purchased - timedelta(days=2) <= stamp <= purchased + timedelta(days=10)
            )
        ]

    timeline = focused.get("get_payment_timeline", {}).get("data")
    current_amounts: list[str] = []
    if isinstance(timeline, dict):
        events = timeline.get("events")
        if isinstance(events, list):
            events = [
                row
                for row in events
                if (
                    (stamp := _date(row.get("event_at"))) is not None
                    and purchased - timedelta(days=1) <= stamp <= purchased + timedelta(days=2)
                )
            ]
            timeline["events"] = events
            current_amounts = [str(row.get("amount_brl")) for row in events]
        payments = timeline.get("payments")
        if isinstance(payments, list) and current_amounts:
            remaining = current_amounts.copy()
            selected = []
            for row in payments:
                amount = str(row.get("payment_value")) if isinstance(row, dict) else ""
                if amount in remaining:
                    selected.append(row)
                    remaining.remove(amount)
            timeline["payments"] = selected

    payments = focused.get("get_order_payments", {}).get("data")
    if isinstance(payments, list) and current_amounts:
        remaining = current_amounts.copy()
        selected = []
        for row in payments:
            amount = str(row.get("payment_value")) if isinstance(row, dict) else ""
            if amount in remaining:
                selected.append(row)
                remaining.remove(amount)
        focused["get_order_payments"]["data"] = selected

    refund = focused.get("get_refund_timeline", {}).get("data")
    if isinstance(refund, dict) and isinstance(refund.get("events"), list):
        refund["events"] = [
            row for row in refund["events"] if in_scope(row, ("event_at",))
        ]

    shipment = focused.get("get_shipment_summary", {}).get("data")
    if isinstance(shipment, dict):
        if isinstance(shipment.get("events"), list):
            shipment["events"] = [
                row for row in shipment["events"] if in_scope(row, ("event_at",))
            ]
        if isinstance(shipment.get("shipping_limits"), list):
            shipment["shipping_limits"] = [
                row
                for row in shipment["shipping_limits"]
                if in_scope(row, ("shipping_limit_at",))
            ]
    return focused


def _verified_issue(evidence: dict[str, dict[str, Any]]) -> tuple[str, float]:
    order = evidence.get("get_order", {}).get("data", {})
    status = str(order.get("order_status", "")).lower() if isinstance(order, dict) else ""
    if status == "canceled":
        return "canceled_order_paid", 0.98
    if status in {"unavailable", "unavailable_order"}:
        return "unavailable_order_paid", 0.98

    refund = evidence.get("get_refund_timeline", {}).get("data", {})
    refund_events = refund.get("events", []) if isinstance(refund, dict) else []
    refund_statuses = {
        str(row.get("status", "")).lower() for row in refund_events if isinstance(row, dict)
    }
    if "failed" in refund_statuses:
        return "refund_failed", 0.98
    if refund_statuses & {"pending", "processing", "requested"}:
        return "refund_pending", 0.96

    shipment = evidence.get("get_shipment_summary", {}).get("data", {})
    shipment_events = shipment.get("events", []) if isinstance(shipment, dict) else []
    delivered = _date(shipment.get("delivered_customer_at")) if isinstance(shipment, dict) else None
    estimated = _date(shipment.get("estimated_delivery_at")) if isinstance(shipment, dict) else None
    if delivered is not None and estimated is not None and delivered > estimated:
        for row in shipment_events:
            if not isinstance(row, dict) or row.get("event_type") != "delivered_late":
                continue
            actor = row.get("actor")
            if actor == "seller":
                return "late_delivery_seller", 0.98
            if actor == "logistics_provider":
                return "late_delivery_logistics", 0.98

    timeline = evidence.get("get_payment_timeline", {}).get("data", {})
    payments = timeline.get("payments", []) if isinstance(timeline, dict) else []
    events = timeline.get("events", []) if isinstance(timeline, dict) else []
    captured = [
        _money(row.get("amount_brl"))
        for row in events
        if isinstance(row, dict) and row.get("event_type") == "captured"
    ]
    items = evidence.get("get_order_items", {}).get("data", [])
    item_total = sum(
        (_money(row.get("price")) + _money(row.get("freight_value"))
         for row in items if isinstance(row, dict)),
        Decimal("0"),
    )
    payment_total = sum(
        (_money(row.get("payment_value")) for row in payments if isinstance(row, dict)),
        Decimal("0"),
    )
    sequences = {
        str(row.get("payment_sequential")) for row in payments if isinstance(row, dict)
    }
    if len(payments) >= 2 and len(sequences) >= 2 and payment_total == item_total:
        return "valid_split_payment", 0.98
    if len(captured) >= 2 and len(set(captured)) < len(captured):
        return "duplicate_charge", 0.98
    if payment_total and item_total and payment_total != item_total:
        return "payment_mismatch", 0.97
    return "unsupported_claim", 0.95


def _normalize_conflicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:5]:
        if not isinstance(item, dict) or not isinstance(item.get("sources"), list):
            continue
        sources = list(dict.fromkeys(
            str(source)[:80] for source in item["sources"] if str(source)
        ))[:5]
        if len(sources) < 2:
            continue
        selected = item.get("selected_source")
        result.append({
            "field": str(item.get("field") or "unknown")[:100],
            "sources": sources,
            "selected_source": str(selected)[:80] if selected is not None else None,
            "resolution_code": str(item.get("resolution_code") or "UNRESOLVED")[:80],
        })
    return result


async def _collect(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]
    available = set(await gateway.list_tools())
    planned = {
        "get_order": {"order_id": order_id},
        "get_order_items": {"order_id": order_id},
        "get_order_payments": {"order_id": order_id},
        "get_payment_timeline": {"order_id": order_id},
        "get_refund_timeline": {"order_id": order_id},
        "get_shipment_summary": {"order_id": order_id},
        "get_sellers": {"order_id": order_id},
        "get_policy": {"policy_version": case["policy_version"]},
    }
    for actor in ("order-agent", "payment-agent", "shipment-agent", "policy-agent"):
        trace.emit(
            case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor,
            decision_code="COLLECT_AUTHORITATIVE_EVIDENCE",
        )

    evidence: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for tool_name, arguments in planned.items():
        if tool_name not in available:
            failures.append(tool_name)
            continue
        actor = ACTORS[tool_name]
        try:
            item = await gateway.call(tool_name, case_id=case_id, **arguments)
        except (RuntimeError, ValueError):
            failures.append(tool_name)
            continue
        evidence[tool_name] = item
        trace.emit(
            case_id=case_id, event_type="tool_result_consumed", actor=actor,
            tool_name=tool_name, evidence_refs=[item["evidence_ref"]],
        )

    for actor in ("order-agent", "payment-agent", "shipment-agent", "policy-agent"):
        actor_refs = [
            item["evidence_ref"] for name, item in evidence.items() if ACTORS[name] == actor
        ]
        trace.emit(
            case_id=case_id, event_type="handoff", actor=actor, target="verifier",
            decision_code="EVIDENCE_READY" if actor_refs else "EVIDENCE_UNAVAILABLE",
            evidence_refs=actor_refs,
        )
    return evidence, failures


def _build_output(
    case: dict[str, Any], evidence_by_tool: dict[str, dict[str, Any]],
    llm_result: dict[str, Any], focused_by_tool: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence = list(evidence_by_tool.values())
    refs = [item["evidence_ref"] for item in evidence]
    issue = llm_result.get("primary_issue")
    confidence = llm_result.get("confidence")
    if issue not in ISSUES:
        issue, confidence = _fallback_issue(evidence_by_tool)
    try:
        confidence = min(1.0, max(0.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5

    policy_data = evidence_by_tool.get("get_policy", {}).get("data", {})
    rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    rule = rules.get(issue, {}) if isinstance(rules, dict) else {}
    if not isinstance(rule, dict) or not rule:
        issue, rule, confidence = "insufficient_evidence", {}, min(confidence, 0.45)

    refund = _money(rule.get("refund_brl", 0)).quantize(Decimal("0.01"))
    action = str(rule.get("recommended_action") or "investigate_missing_evidence")
    case_status = str(rule.get("case_status") or "needs_investigation")
    raw_parties = rule.get("responsible_parties") or [
        {"party_type": "unknown", "party_id": None}
    ]
    parties = [
        {"party_type": party.get("party_type", "unknown"), "party_id": party.get("party_id")}
        for party in raw_parties[:5] if isinstance(party, dict)
    ] or [{"party_type": "unknown", "party_id": None}]

    payment_source = focused_by_tool or evidence_by_tool
    payment_data = payment_source.get("get_order_payments", {}).get("data", [])
    total_paid = sum(
        (_money(row.get("payment_value")) for row in payment_data if isinstance(row, dict)),
        Decimal("0"),
    )
    request_verdict = (
        "unsupported" if refund == 0 else
        "supported" if total_paid and refund == total_paid else "partially_supported"
    )
    claim_assessments = []
    for claim in case["customer_request"].get("claims", [])[:5]:
        topic = claim.get("topic")
        verdict = request_verdict if topic == "requested_full_refund" else (
            "supported" if topic == issue else "unsupported"
        )
        claim_assessments.append({
            "claim_id": str(claim["claim_id"])[:64], "verdict": verdict,
            "confidence": confidence, "evidence_refs": refs,
        })

    order_ids = _known_ids(evidence, "order_id")
    claimed_order = case["customer_request"]["claimed_order_id"]
    if claimed_order not in order_ids:
        order_ids = [claimed_order] if not evidence else order_ids
    refund_lines = []
    if refund > 0:
        refund_lines.append({
            "reason_code": action.upper()[:80], "amount_brl": float(refund),
            "entity_id": order_ids[0] if order_ids else None,
        })

    return {
        "schema_version": "day09-l3a-output-v2", "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue, "case_status": case_status, "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": order_ids, "item_ids": _known_ids(evidence, "order_item_id"),
            "seller_ids": _known_ids(evidence, "seller_id"),
            "payment_references": _known_ids(evidence, "payment_reference"),
            "shipment_ids": _known_ids(evidence, "shipment_id"),
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": refs,
        # Conflicts are omitted unless a deterministic source comparator is available.
        # A model-generated disagreement alone is not authoritative evidence.
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    evidence, failures = await _collect(case, gateway, trace)
    focused = _focused_evidence(evidence)
    settings = Settings.load()
    llm = OpenRouterClient(
        settings.openrouter_api_key, settings.openrouter_base_url, settings.openrouter_model
    )
    try:
        llm_result = await llm.complete_json(
            system=SYSTEM_PROMPT,
            user={
                "case": case,
                "mcp_evidence": {
                    name: item for name, item in focused.items() if name != "get_policy"
                },
                "unavailable_tools": failures,
            },
        )
        decision_code = str(
            llm_result.get("rationale_code") or "LLM_EVIDENCE_SYNTHESIS"
        )[:80]
    except (OSError, ValueError, httpx2.HTTPError):
        issue, confidence = _fallback_issue(focused)
        llm_result = {
            "primary_issue": issue, "confidence": confidence, "data_conflicts": [],
        }
        decision_code = "DETERMINISTIC_FALLBACK"

    verified_issue, verified_confidence = _verified_issue(focused)
    llm_agreed = llm_result.get("primary_issue") == verified_issue
    llm_result["primary_issue"] = verified_issue
    llm_result["confidence"] = verified_confidence
    decision_code = "LLM_VERIFIED" if llm_agreed else "VERIFIER_CORRECTED_LLM"

    output = _build_output(case, evidence, llm_result, focused)
    trace.emit(
        case_id=case_id, event_type="policy_decided", actor="policy-agent",
        target="verifier", decision_code=output["assessment"]["primary_issue"].upper(),
        evidence_refs=[evidence["get_policy"]["evidence_ref"]]
        if "get_policy" in evidence else [],
    )
    trace.emit(
        case_id=case_id, event_type="verification_completed", actor="verifier",
        target="coordinator", decision_code=decision_code,
        evidence_refs=output["evidence_refs"],
        attributes={"model": settings.openrouter_model, "mcp_failures": len(failures)},
    )
    return output
