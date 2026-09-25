from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# ponytail: policy's recommended_action / case_status per primary_issue is a fixed
# global lookup table (verified identical across cases), so get_policy is used as
# the authority for those two fields. refund_brl / responsible party_id are
# recomputed per case from real evidence instead of the policy template's constants.

# ponytail: every case mixes the real lifecycle (captures at purchase, refunds ~est+1d,
# shipment events on delivery day) with a decoy block shifted by >=9 days, so windows
# are anchored to this order's own dates: payment events must sit on the purchase day,
# refunds/shipment events inside purchase..delivery/estimate. A decoy shifted <1 day
# would slip through; tighten with per-block grouping if that ever shows up.
_WINDOW_BEFORE = timedelta(days=1)
_PAYMENT_WINDOW = timedelta(days=1)
_WINDOW_AFTER_LIFECYCLE = timedelta(days=2)
_LATE_EVENT_TOLERANCE = timedelta(days=2)

# ponytail: get_sellers / get_customer_history / get_product_context are deliberately
# left out of every entry below (and never fetched in solve_case) until the MCP
# Evidence Gateway is confirmed reachable again and we can verify their response shape
# and the forbidden-domain evidence penalty ARCHITECTURE.md §2 warns about. Add them
# back per-issue only once that's confirmed.
ISSUE_TOOLS: dict[str, list[str]] = {
    "canceled_order_paid": ["get_order", "get_payment_timeline", "get_policy"],
    "unavailable_order_paid": [
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_policy",
    ],
    "late_delivery_seller": ["get_order", "get_order_items", "get_shipment_summary", "get_policy"],
    "late_delivery_logistics": ["get_order", "get_shipment_summary", "get_policy"],
    "valid_split_payment": ["get_order", "get_order_items", "get_payment_timeline", "get_policy"],
    "payment_mismatch": ["get_order", "get_payment_timeline", "get_policy"],
    "duplicate_charge": ["get_order", "get_payment_timeline", "get_policy"],
    "refund_pending": ["get_order", "get_refund_timeline", "get_policy"],
    "refund_failed": ["get_order", "get_refund_timeline", "get_policy"],
    "unsupported_claim": [
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    ],
    "insufficient_evidence": [
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    ],
}


def _money(value: str) -> float:
    return round(float(value), 2)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _in_window(moment: datetime, start: datetime, end: datetime) -> bool:
    return start - _WINDOW_BEFORE <= moment <= end + _WINDOW_AFTER_LIFECYCLE


async def _fetch(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    tool_name: str,
    **kwargs: str,
) -> dict[str, Any] | None:
    try:
        evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
    except RuntimeError:
        return None
    if os.environ.get("DUMP_EVIDENCE") == "1":
        dump_path = Path("debug") / case_id / f"{tool_name}.json"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
    )
    return evidence


def _classify(
    order: dict[str, Any],
    items: list[dict[str, Any]],
    shipment: dict[str, Any],
    pay_timeline: dict[str, Any],
    refund_timeline: dict[str, Any] | None,
) -> dict[str, Any]:
    purchase_at = _dt(order["order_purchase_timestamp"])
    delivered_customer_at = _dt(order["order_delivered_customer_date"])
    estimated_at = _dt(order["order_estimated_delivery_date"])
    delivered_carrier_at = _dt(order["order_delivered_carrier_date"])
    order_status = order["order_status"]
    lifecycle_end = max(d for d in (purchase_at, delivered_customer_at, estimated_at) if d)

    def in_lifecycle(value: str) -> bool:
        return _in_window(_dt(value), purchase_at, lifecycle_end)

    conflicts: list[dict[str, Any]] = []

    def result(issue: str, refund: float, confidence: float) -> dict[str, Any]:
        return {
            "primary_issue": issue,
            "refund_brl": refund,
            "seller_id": seller_id,
            "confidence": confidence,
            "conflicts": conflicts,
            "good_items": good_items,
        }

    # ponytail: a repeated order_item_id is a duplicate/reissued record, not a second
    # line item, so keep only the copy whose shipping_limit_date is closest to purchase.
    by_item_id: dict[str, dict[str, Any]] = {}
    for i in items:
        key = i["order_item_id"]
        if key not in by_item_id or abs(_dt(i["shipping_limit_date"]) - purchase_at) < abs(
            _dt(by_item_id[key]["shipping_limit_date"]) - purchase_at
        ):
            by_item_id[key] = i
    good_items = list(by_item_id.values())
    if len(good_items) != len(items):
        conflicts.append(
            {
                "field": "order_items",
                "sources": ["get_order_items", "order_purchase_timestamp"],
                "selected_source": "get_order_items",
                "resolution_code": "excluded_duplicate_order_item_id_records",
            }
        )
    item_total = round(sum(_money(i["price"]) + _money(i["freight_value"]) for i in good_items), 2)
    freight_total = round(sum(_money(i["freight_value"]) for i in good_items), 2)
    seller_id = good_items[0]["seller_id"] if good_items else None

    pay_events = pay_timeline["events"]
    # A byte-identical event (same instant, type and amount) is a replicated record,
    # not a second charge: keep one copy.
    good_pay_events = list(
        {
            (e["event_at"], e["event_type"], e["amount_brl"]): e
            for e in pay_events
            if abs(_dt(e["event_at"]) - purchase_at) <= _PAYMENT_WINDOW
        }.values()
    )
    if len(good_pay_events) != len(pay_events):
        conflicts.append(
            {
                "field": "payment_events",
                "sources": ["get_payment_timeline", "order_purchase_timestamp"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "excluded_payment_events_outside_order_lifecycle_window",
            }
        )
    good_captured = [e for e in good_pay_events if e["event_type"] == "captured"]
    captured_total = round(sum(_money(e["amount_brl"]) for e in good_captured), 2)
    mismatches = [
        e
        for e in good_pay_events
        if e["event_type"] == "reconciliation_mismatch" and e["status"] == "open"
    ]

    is_late = bool(delivered_customer_at and estimated_at and delivered_customer_at > estimated_at)
    late_events = [e for e in shipment["events"] if in_lifecycle(e["event_at"])]
    if len(late_events) != len(shipment["events"]) or bool(late_events) != is_late:
        conflicts.append(
            {
                "field": "delivery_timeliness",
                "sources": ["order_delivery_dates", "shipment_delivered_late_event"],
                "selected_source": "order_delivery_dates",
                "resolution_code": "order_dates_authoritative_over_shipment_event",
            }
        )

    refund_events: list[dict[str, Any]] = []
    if refund_timeline is not None:
        # A real refund returns money this lifecycle actually captured.
        captured_amounts = {_money(e["amount_brl"]) for e in good_captured}
        refund_events = [
            e
            for e in refund_timeline["events"]
            if in_lifecycle(e["event_at"]) and _money(e["amount_brl"]) in captured_amounts
        ]
        if len(refund_events) != len(refund_timeline["events"]):
            conflicts.append(
                {
                    "field": "refund_events",
                    "sources": ["get_refund_timeline", "order_purchase_timestamp"],
                    "selected_source": "get_refund_timeline",
                    "resolution_code": "excluded_refund_events_outside_order_lifecycle_window",
                }
            )

    if mismatches:
        return result(
            "payment_mismatch", round(sum(_money(e["amount_brl"]) for e in mismatches), 2), 0.95
        )

    if order_status in ("canceled", "unavailable") and captured_total > 0:
        issue = "canceled_order_paid" if order_status == "canceled" else "unavailable_order_paid"
        return result(issue, captured_total, 0.95)

    if is_late:
        matching = [
            e
            for e in late_events
            if abs(_dt(e["event_at"]) - delivered_customer_at) <= _LATE_EVENT_TOLERANCE
        ]
        if matching:
            actor = matching[0]["actor"]
            confidence = 0.9
        else:
            shipping_limits = [_dt(i["shipping_limit_date"]) for i in good_items]
            actor = (
                "seller"
                if delivered_carrier_at
                and shipping_limits
                and delivered_carrier_at > min(shipping_limits)
                else "logistics_provider"
            )
            confidence = 0.65
        issue = "late_delivery_seller" if actor == "seller" else "late_delivery_logistics"
        # ponytail: freight refund is capped at what this lifecycle actually captured
        # (logistics cases capture less than the item freight); matches get_policy's refund_brl.
        refund = min(freight_total, captured_total) if captured_total > 0 else freight_total
        return result(issue, refund, confidence)

    if refund_events:
        latest = max(refund_events, key=lambda e: _dt(e["event_at"]))
        if latest["status"] == "failed":
            return result("refund_failed", _money(latest["amount_brl"]), 0.95)
        if latest["status"] == "pending":
            return result("refund_pending", 0.0, 0.9)

    # The same amount captured more than once within this lifecycle, with the order
    # overpaid, is one charge taken twice: refund the extra copies.
    amount_counts: dict[float, int] = {}
    for e in good_captured:
        amount = _money(e["amount_brl"])
        amount_counts[amount] = amount_counts.get(amount, 0) + 1
    duplicate_amount = round(sum(a * (n - 1) for a, n in amount_counts.items() if n > 1), 2)
    if duplicate_amount > 0 and captured_total > item_total + 0.01:
        return result("duplicate_charge", duplicate_amount, 0.95)

    if abs(captured_total - item_total) <= 0.01:
        if len(good_captured) > 1 or len({p["payment_type"] for p in pay_timeline["payments"]}) > 1:
            return result("valid_split_payment", 0.0, 0.9)
        return result("unsupported_claim", 0.0, 0.9)

    return result("insufficient_evidence", 0.0, 0.5)


def _claim_verdict(topic: str, primary_issue: str, case_status: str, refund_brl: float) -> str:
    if topic == "requested_full_refund":
        if case_status == "action_required" and refund_brl > 0:
            return "supported"
        if case_status == "needs_investigation":
            return "partially_supported"
        return "unsupported"
    if topic == primary_issue:
        return "supported"
    if primary_issue == "insufficient_evidence":
        return "insufficient_evidence"
    return "unsupported"


def _verify(output: dict[str, Any], fetched_refs: set[str]) -> list[str]:
    """Cross-field + provenance sanity checks. Returns problem codes (empty = passed)."""
    problems: list[str] = []
    if not set(output["evidence_refs"]).issubset(fetched_refs):
        problems.append("evidence_ref_not_fetched")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]).issubset(fetched_refs):
            problems.append("claim_evidence_ref_not_fetched")

    case_status = output["assessment"]["case_status"]
    financial = output["financial_resolution"]
    if case_status == "no_action" and (
        financial["recommended_refund_brl"] != 0 or financial["refund_lines"]
    ):
        problems.append("no_action_with_refund")
    if case_status == "action_required" and financial["recommended_refund_brl"] <= 0:
        problems.append("action_required_without_refund")

    party = output["root_cause_analysis"]["responsible_parties"][0]
    if party["party_type"] == "seller" and party["party_id"] is None:
        problems.append("seller_party_missing_id")

    actions = output["resolution_actions"]
    if len(actions) != len(set(actions)):
        problems.append("duplicate_resolution_actions")
    return problems


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3A coordinator: dispatches specialist agents over MCP evidence, classifies
    the primary issue from evidence (never from the customer's own claim), then asks
    the policy agent for the authoritative case_status/action before a verifier pass.
    """
    case_id = case["case_id"]
    request = case["customer_request"]
    order_id = request["claimed_order_id"]
    policy_version = case["policy_version"]

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="order-agent"
    )
    order_ev = await _fetch(gateway, trace, case_id, "order-agent", "get_order", order_id=order_id)
    items_ev = await _fetch(
        gateway, trace, case_id, "order-agent", "get_order_items", order_id=order_id
    )

    if order_ev is None or items_ev is None:
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            decision_code="insufficient_evidence",
        )
        policy_ev = await _fetch(
            gateway, trace, case_id, "policy-agent", "get_policy", policy_version=policy_version
        )
        refs = [ev["evidence_ref"] for ev in (order_ev, policy_ev) if ev is not None]
        trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="verifier")
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="failed",
            attributes={"problems": 1},
        )
        return _insufficient_evidence_output(case_id, order_id, refs, request["claims"])

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="payment-agent"
    )
    pay_timeline_ev = await _fetch(
        gateway, trace, case_id, "payment-agent", "get_payment_timeline", order_id=order_id
    )

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="shipment-agent"
    )
    shipment_ev = await _fetch(
        gateway, trace, case_id, "shipment-agent", "get_shipment_summary", order_id=order_id
    )

    refund_ev = await _fetch(
        gateway, trace, case_id, "payment-agent", "get_refund_timeline", order_id=order_id
    )

    result = _classify(
        order_ev["data"],
        items_ev["data"],
        shipment_ev["data"],
        pay_timeline_ev["data"],
        refund_ev["data"] if refund_ev else None,
    )
    primary_issue = result["primary_issue"]

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
    )
    policy_ev = await _fetch(
        gateway, trace, case_id, "policy-agent", "get_policy", policy_version=policy_version
    )
    rule = (policy_ev["data"]["rules"].get(primary_issue) if policy_ev else None) or {
        "case_status": "needs_investigation",
        "recommended_action": "request_more_evidence",
        "responsible_parties": [{"party_type": "unknown", "party_id": None}],
    }
    case_status = rule["case_status"]
    party_type = rule["responsible_parties"][0]["party_type"]
    party_id = result["seller_id"] if party_type == "seller" else None

    # ponytail: refs_by_tool + ISSUE_TOOLS keeps evidence_refs precise (only the tools
    # that actually back this primary_issue), instead of citing every fetched domain.
    refs_by_tool = {
        tool_name: ev["evidence_ref"]
        for tool_name, ev in (
            ("get_order", order_ev),
            ("get_order_items", items_ev),
            ("get_payment_timeline", pay_timeline_ev),
            ("get_shipment_summary", shipment_ev),
            ("get_refund_timeline", refund_ev),
            ("get_policy", policy_ev),
        )
        if ev is not None
    }
    fetched_refs = set(refs_by_tool.values())

    def _refs_for(issue: str) -> list[str]:
        return [refs_by_tool[t] for t in ISSUE_TOOLS[issue] if t in refs_by_tool]

    evidence_refs = _refs_for(primary_issue)
    refund_refs = [
        refs_by_tool[t]
        for t in ("get_payment_timeline", "get_refund_timeline", "get_policy")
        if t in refs_by_tool
    ]

    claim_assessments = [
        {
            "claim_id": claim["claim_id"],
            "verdict": _claim_verdict(
                claim["topic"], primary_issue, case_status, result["refund_brl"]
            ),
            "confidence": result["confidence"],
            "evidence_refs": (
                refund_refs if claim["topic"] == "requested_full_refund" else evidence_refs
            ),
        }
        for claim in request["claims"]
    ]

    good_items = result["good_items"]
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": result["confidence"],
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": sorted({i["order_item_id"] for i in good_items}),
            "seller_ids": sorted({i["seller_id"] for i in good_items}),
            # ponytail: no per-payment or per-shipment id field has been confirmed in a
            # live evidence dump yet (MCP gateway unreachable), so these stay empty
            # rather than invented (`pay-{order_id}-{i}` / `[order_id]` previously).
            # Fill in once get_payment_timeline/get_shipment_summary dumps are read.
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": result["conflicts"],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": result["refund_brl"],
            "refund_lines": (
                [
                    {
                        "reason_code": primary_issue,
                        "amount_brl": result["refund_brl"],
                        "entity_id": order_id,
                    }
                ]
                if result["refund_brl"] > 0
                else []
            ),
        },
        "resolution_actions": [rule["recommended_action"]],
    }

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="verifier")
    problems = _verify(output, fetched_refs)
    if problems:
        output["assessment"]["primary_issue"] = "insufficient_evidence"
        output["assessment"]["case_status"] = "needs_investigation"
        output["assessment"]["confidence"] = 0.3
        output["evidence_refs"] = _refs_for("insufficient_evidence")
        output["root_cause_analysis"] = {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        }
        output["financial_resolution"]["recommended_refund_brl"] = 0.0
        output["financial_resolution"]["refund_lines"] = []
        output["resolution_actions"] = ["request_more_evidence"]
        for claim in output["claim_assessments"]:
            claim["verdict"] = "insufficient_evidence"
            claim["confidence"] = 0.3
            claim["evidence_refs"] = output["evidence_refs"]
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="failed" if problems else "passed",
        attributes={"problems": len(problems)},
    )
    return output


def _insufficient_evidence_output(
    case_id: str, order_id: str, refs: list[str], claims: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.3,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "insufficient_evidence",
                "confidence": 0.3,
                "evidence_refs": refs,
            }
            for claim in claims
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["request_more_evidence"],
    }
