"""Policy engine: turn collected MCP evidence into a case decision.

Pure and synchronous (no MCP calls, no trace writes) so the workflow can call it
from its policy node and the verifier/tests can exercise it with fixtures.

Expected ``evidence`` is a mapping ``tool_name -> MCP evidence envelope`` for the
tools the workflow managed to call. Missing tools are allowed:

- ``get_order``             (required, otherwise ``insufficient_evidence``)
- ``get_order_items``       seller ids, order total
- ``get_shipment_summary``  delivery timestamps and late-delivery events
- ``get_payment_timeline``  captured / mismatch events (``get_order_payments`` as fallback)
- ``get_refund_timeline``   refund events (the server errors when an order has none)
- ``get_policy``            action / case_status / party type per issue

Every MCP source mixes in rows from outside the case window. Only rows whose
timestamp lies between the order purchase and ``case.opened_at`` are trusted;
the rest are reported as data conflicts instead of driving the decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Fallback when get_policy is unavailable; mirrors EC_POLICY_V1.
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "party_type": "platform",
    },
    "unavailable_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "party_type": "seller",
    },
    "late_delivery_seller": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "party_type": "seller",
    },
    "late_delivery_logistics": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "party_type": "logistics_provider",
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "party_type": "customer",
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "recommended_action": "reconcile_payment",
        "party_type": "payment_provider",
    },
    "duplicate_charge": {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "party_type": "payment_provider",
    },
    "refund_pending": {
        "case_status": "needs_investigation",
        "recommended_action": "monitor_refund",
        "party_type": "payment_provider",
    },
    "refund_failed": {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "party_type": "payment_provider",
    },
    "unsupported_claim": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "party_type": "customer",
    },
}
INSUFFICIENT = "insufficient_evidence"

# Which MCP sources justify each issue (evidence precision matters for scoring).
ISSUE_SOURCES: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_payment_timeline", "get_order_payments"),
    "unavailable_order_paid": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_order_payments",
    ),
    "late_delivery_seller": ("get_order", "get_order_items", "get_shipment_summary"),
    "late_delivery_logistics": ("get_order", "get_shipment_summary"),
    "valid_split_payment": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_order_payments",
    ),
    "payment_mismatch": ("get_order", "get_payment_timeline", "get_order_payments"),
    "duplicate_charge": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_order_payments",
    ),
    "refund_pending": ("get_order", "get_refund_timeline"),
    "refund_failed": ("get_order", "get_refund_timeline"),
    "unsupported_claim": (
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_payment_timeline",
    ),
    INSUFFICIENT: ("get_order",),
}
CAUSE_CODES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_CAPTURE",
    "unavailable_order_paid": "SELLER_ITEM_UNAVAILABLE",
    "late_delivery_seller": "SELLER_LATE_HANDOFF",
    "late_delivery_logistics": "CARRIER_DELIVERY_DELAY",
    "valid_split_payment": "SPLIT_PAYMENT_EXPECTED",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_CAPTURE",
    "refund_pending": "REFUND_IN_PROGRESS",
    "refund_failed": "REFUND_PROCESSING_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
    INSUFFICIENT: "MISSING_AUTHORITATIVE_EVIDENCE",
}


@dataclass
class Facts:
    """Evidence-derived facts inside the case window."""

    order_status: str | None = None
    purchase_at: datetime | None = None
    carrier_at: datetime | None = None
    delivered_at: datetime | None = None
    estimated_at: datetime | None = None
    seller_ids: list[str] = field(default_factory=list)
    shipping_limits: list[datetime] = field(default_factory=list)
    order_total: float | None = None
    captures: list[float] = field(default_factory=list)
    capture_types: list[str] = field(default_factory=list)
    mismatch_amounts: list[float] = field(default_factory=list)
    refund_events: list[dict[str, Any]] = field(default_factory=list)
    late_actors: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _money(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _data(evidence: Mapping[str, Any], tool: str) -> Any:
    envelope = evidence.get(tool)
    return envelope.get("data") if isinstance(envelope, Mapping) else None


def _rows(value: Any) -> list[dict[str, Any]]:
    """Dict rows of a list, with byte-identical duplicates removed (source replication)."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        key = repr(sorted(row.items()))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def _conflict(facts: Facts, field_name: str, sources: list[str], code: str) -> None:
    sources = list(dict.fromkeys(sources))
    if len(sources) < 2 or len(facts.conflicts) >= 5:
        return
    if any(c["field"] == field_name for c in facts.conflicts):
        return
    facts.conflicts.append(
        {
            "field": field_name,
            "sources": sources,
            "selected_source": sources[0],
            "resolution_code": code,
        }
    )


def extract_facts(case: Mapping[str, Any], evidence: Mapping[str, Any]) -> Facts | None:
    order = _data(evidence, "get_order")
    if not isinstance(order, dict):
        return None
    facts = Facts(
        order_status=order.get("order_status"),
        purchase_at=_ts(order.get("order_purchase_timestamp")),
        carrier_at=_ts(order.get("order_delivered_carrier_date")),
        delivered_at=_ts(order.get("order_delivered_customer_date")),
        estimated_at=_ts(order.get("order_estimated_delivery_date")),
    )
    opened_at = _ts(case.get("opened_at"))

    def in_window(value: Any) -> bool:
        moment = _ts(value)
        if moment is None:
            return False
        if facts.purchase_at and moment < facts.purchase_at:
            return False
        return not (opened_at and moment > opened_at)

    # Items: keep rows whose seller handoff limit is inside the case window.
    items = _rows(_data(evidence, "get_order_items"))
    scoped_items = [row for row in items if in_window(row.get("shipping_limit_date"))] or items[:1]
    if len(scoped_items) < len(items):
        _conflict(
            facts,
            "order_items.shipping_limit_date",
            ["case_window", "get_order_items"],
            "out_of_window_rows_ignored",
        )
    for row in scoped_items:
        seller = row.get("seller_id")
        if isinstance(seller, str) and seller and seller not in facts.seller_ids:
            facts.seller_ids.append(seller)
        limit = _ts(row.get("shipping_limit_date"))
        if limit:
            facts.shipping_limits.append(limit)
    if scoped_items:
        facts.order_total = round(
            sum(_money(r.get("price")) + _money(r.get("freight_value")) for r in scoped_items), 2
        )

    # Shipment: late-delivery events that belong to this delivery. The customer may
    # open the case before the parcel arrives, so an event on the actual delivery
    # date counts even when it is after opened_at.
    shipment = _data(evidence, "get_shipment_summary")
    if isinstance(shipment, dict):
        if facts.delivered_at is None:
            facts.delivered_at = _ts(shipment.get("delivered_customer_at"))
        for event in _rows(shipment.get("events")):
            if event.get("event_type") != "delivered_late" or event.get("status") != "confirmed":
                continue
            moment = _ts(event.get("event_at"))
            on_delivery = bool(
                moment and facts.delivered_at and moment.date() == facts.delivered_at.date()
            )
            if on_delivery or in_window(event.get("event_at")):
                facts.late_actors.append(str(event.get("actor") or "unknown"))
            else:
                _conflict(
                    facts,
                    "shipment.events",
                    ["case_window", "get_shipment_summary"],
                    "out_of_window_event_ignored",
                )
        if (
            shipment.get("order_status")
            and facts.order_status
            and shipment["order_status"] != facts.order_status
        ):
            _conflict(
                facts,
                "order_status",
                ["get_order", "get_shipment_summary"],
                "order_record_authoritative",
            )

    # Payments: prefer the lifecycle timeline, fall back to raw payment rows.
    timeline = _data(evidence, "get_payment_timeline")
    if isinstance(timeline, dict):
        for event in _rows(timeline.get("events")):
            if not in_window(event.get("event_at")):
                continue
            kind, amount = event.get("event_type"), _money(event.get("amount_brl"))
            if kind == "captured" and event.get("status") == "confirmed":
                facts.captures.append(amount)
            elif kind == "reconciliation_mismatch" and event.get("status") != "resolved":
                facts.mismatch_amounts.append(amount)
        in_scope = {c for c in facts.captures}
        facts.capture_types = [
            str(p.get("payment_type"))
            for p in _rows(timeline.get("payments"))
            if _money(p.get("payment_value")) in in_scope
        ]
        if len(_rows(timeline.get("events"))) > len(facts.captures) + len(facts.mismatch_amounts):
            _conflict(
                facts,
                "payments.events",
                ["case_window", "get_payment_timeline"],
                "out_of_window_event_ignored",
            )
    else:
        rows = _rows(_data(evidence, "get_order_payments"))
        facts.captures = [_money(p.get("payment_value")) for p in rows]
        facts.capture_types = [str(p.get("payment_type")) for p in rows]

    refunds = _data(evidence, "get_refund_timeline")
    refund_rows = _rows(refunds.get("events")) if isinstance(refunds, dict) else _rows(refunds)
    for event in refund_rows:
        if in_window(event.get("event_at")):
            facts.refund_events.append(event)
        else:
            _conflict(
                facts,
                "refund.events",
                ["case_window", "get_refund_timeline"],
                "out_of_window_event_ignored",
            )
    return facts


def classify(facts: Facts) -> tuple[str, float]:
    """Return ``(primary_issue, refund_brl)`` from in-window facts.

    Order matters: explicit lifecycle signals (refund, mismatch) outrank order
    status, which outranks delivery timing; absence of any problem means the
    claim is unsupported.
    """
    captured = round(sum(facts.captures), 2)
    for event in reversed(facts.refund_events):
        status = str(event.get("status", "")).lower()
        amount = _money(event.get("amount_brl"))
        if status == "failed":
            return "refund_failed", amount or captured
        if status in {"pending", "requested", "processing"}:
            return "refund_pending", 0.0
    if facts.mismatch_amounts:
        return "payment_mismatch", facts.mismatch_amounts[0]
    if facts.order_status == "canceled" and captured > 0:
        return "canceled_order_paid", captured
    if facts.order_status == "unavailable" and captured > 0:
        return "unavailable_order_paid", captured
    if len(facts.captures) >= 2:
        duplicated = [a for a in set(facts.captures) if facts.captures.count(a) > 1]
        total_ok = facts.order_total is not None and abs(captured - facts.order_total) < 0.01
        if duplicated and not total_ok:
            return "duplicate_charge", max(duplicated)
        if total_ok:
            return "valid_split_payment", 0.0
    late = facts.delivered_at and facts.estimated_at and facts.delivered_at > facts.estimated_at
    if facts.late_actors or late:
        # Seller missed its handoff limit -> seller; otherwise the carrier was late.
        seller_late = bool(
            facts.carrier_at
            and facts.shipping_limits
            and facts.carrier_at > min(facts.shipping_limits)
        )
        actor = (
            facts.late_actors[-1]
            if facts.late_actors
            else ("seller" if seller_late else "logistics_provider")
        )
        issue = "late_delivery_seller" if actor == "seller" else "late_delivery_logistics"
        return issue, captured if len(facts.captures) == 1 else 0.0
    return "unsupported_claim", 0.0


def _rules(evidence: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    data = _data(evidence, "get_policy")
    rules = data.get("rules") if isinstance(data, dict) else None
    merged = {issue: dict(rule) for issue, rule in DEFAULT_RULES.items()}
    if isinstance(rules, dict):
        for issue, rule in rules.items():
            if not isinstance(rule, dict):
                continue
            base = merged.setdefault(issue, {})
            for key in ("case_status", "recommended_action", "refund_brl"):
                if key in rule:
                    base[key] = rule[key]
            parties = _rows(rule.get("responsible_parties"))
            if parties and parties[0].get("party_type"):
                base["party_type"] = parties[0]["party_type"]
    return merged


def _refs(evidence: Mapping[str, Any], tools: tuple[str, ...]) -> list[str]:
    refs: list[str] = []
    for tool in (*tools, "get_policy"):
        envelope = evidence.get(tool)
        ref = envelope.get("evidence_ref") if isinstance(envelope, Mapping) else None
        if isinstance(ref, str) and ref not in refs:
            refs.append(ref)
    return refs


def _claims(
    case: Mapping[str, Any],
    issue: str,
    refund: float,
    facts: Facts | None,
    confidence: float,
    refs: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    claims = case.get("customer_request", {}).get("claims", [])
    for claim in _rows(claims)[:5]:
        topic, claim_id = claim.get("topic"), claim.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        if issue == INSUFFICIENT:
            verdict = "insufficient_evidence"
        elif topic == "requested_full_refund":
            total = facts.order_total if facts else None
            if refund <= 0:
                verdict = "unsupported"
            elif total is not None and refund + 0.01 >= total:
                verdict = "supported"
            else:
                verdict = "partially_supported"
        else:
            verdict = "supported" if topic == issue else "unsupported"
        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    return result


def decide(case: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Build the policy-owned part of the output.

    Returns the keys ``assessment``, ``claim_assessments``, ``root_cause_analysis``,
    ``data_conflicts``, ``financial_resolution``, ``resolution_actions`` and
    ``evidence_refs`` (only the refs that support the decision). The workflow
    adds ``schema_version``, ``case_id`` and ``affected_entities``.
    """
    facts = extract_facts(case, evidence)
    rules = _rules(evidence)
    if facts is None:
        issue, refund, rule = INSUFFICIENT, 0.0, {}
        status, action, party_type = "needs_investigation", "collect_missing_evidence", "unknown"
    else:
        issue, refund = classify(facts)
        rule = rules.get(issue, {})
        status = rule.get("case_status", "needs_investigation")
        action = rule.get("recommended_action", "collect_missing_evidence")
        party_type = rule.get("party_type", "unknown")
        if refund <= 0 and status == "action_required":
            refund = _money(rule.get("refund_brl"))
    if status != "action_required":
        refund = 0.0

    party_id = None
    if party_type == "seller" and facts and facts.seller_ids:
        party_id = facts.seller_ids[0]

    refs = _refs(evidence, ISSUE_SOURCES.get(issue, ("get_order",)))
    claimed = {c.get("topic") for c in _rows(case.get("customer_request", {}).get("claims", []))}
    if issue == INSUFFICIENT:
        confidence = 0.3
    elif issue in claimed:
        confidence = 0.9
    else:
        confidence = 0.75

    relevant = set(ISSUE_SOURCES.get(issue, ()))
    conflicts = [c for c in (facts.conflicts if facts else []) if relevant & set(c["sources"])]
    if (
        facts
        and issue != INSUFFICIENT
        and claimed - {"requested_full_refund"}
        and issue not in claimed
    ):
        conflicts = conflicts[:4]
        conflicts.append(
            {
                "field": "primary_issue",
                "sources": ["mcp_evidence", "customer_claim"],
                "selected_source": "mcp_evidence",
                "resolution_code": "evidence_over_customer_claim",
            }
        )

    entity_id = party_id or (
        facts.seller_ids[0] if facts and facts.seller_ids and issue.startswith("late") else None
    )
    if entity_id is None:
        order = _data(evidence, "get_order")
        entity_id = order.get("order_id") if isinstance(order, dict) else None
    refund_lines = (
        [{"reason_code": action, "amount_brl": refund, "entity_id": entity_id}]
        if refund > 0
        else []
    )

    return {
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "claim_assessments": _claims(case, issue, refund, facts, confidence, refs),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": CAUSE_CODES.get(issue, "UNKNOWN_CAUSE"), "rank": 1}],
            "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }
