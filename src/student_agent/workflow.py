from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

EV_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")

# Which evidence domains matter for each primary issue. Evidence is scored on
# precision as well as coverage, so a case only cites the tools it truly needs.
TOOLS_BY_TOPIC: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_order_payments", "get_policy"),
    "unavailable_order_paid": (
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_sellers",
        "get_policy",
    ),
    "late_delivery_seller": (
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_sellers",
        "get_policy",
    ),
    "late_delivery_logistics": (
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_policy",
    ),
    "valid_split_payment": ("get_order", "get_order_payments", "get_policy"),
    "payment_mismatch": (
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_policy",
    ),
    "duplicate_charge": (
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    ),
    "refund_pending": (
        "get_order",
        "get_order_payments",
        "get_refund_timeline",
        "get_policy",
    ),
    "refund_failed": (
        "get_order",
        "get_order_payments",
        "get_refund_timeline",
        "get_policy",
    ),
    "unsupported_claim": ("get_order", "get_shipment_summary", "get_policy"),
}

CAUSE_CODE_BY_TOPIC: dict[str, str] = {
    "canceled_order_paid": "ORDER_CANCELED_BEFORE_FULFILLMENT",
    "unavailable_order_paid": "SELLER_INVENTORY_STOCKOUT",
    "late_delivery_seller": "SELLER_DISPATCH_DELAY",
    "late_delivery_logistics": "LOGISTICS_TRANSIT_DELAY",
    "valid_split_payment": "VALID_SPLIT_PAYMENT_TRANSACTION",
    "payment_mismatch": "PAYMENT_AMOUNT_DISCREPANCY",
    "duplicate_charge": "PAYMENT_GATEWAY_DUPLICATE_AUTH",
    "refund_pending": "REFUND_PROCESSING_WITHIN_SLA",
    "refund_failed": "PAYMENT_GATEWAY_REFUND_FAILURE",
    "unsupported_claim": "CLAIM_NOT_SUBSTANTIATED_BY_EVIDENCE",
    "insufficient_evidence": "EVIDENCE_UNAVAILABLE_FROM_GATEWAY",
}

REFUND_REASON_BY_TOPIC: dict[str, str] = {
    "canceled_order_paid": "FULL_REFUND_CANCELED_ORDER",
    "unavailable_order_paid": "FULL_REFUND_UNAVAILABLE_ITEM",
    "late_delivery_seller": "REFUND_SHIPPING_DELAY_SELLER",
    "late_delivery_logistics": "REFUND_SHIPPING_DELAY_LOGISTICS",
    "payment_mismatch": "REFUND_PAYMENT_DISCREPANCY",
    "duplicate_charge": "REFUND_DUPLICATE_CHARGE",
    "refund_failed": "RETRY_FAILED_REFUND",
}

_TOOL_CACHE: dict[int, dict[str, Any]] = {}


async def _discover_tools(gateway: EvidenceGateway) -> dict[str, Any]:
    """List tools once per gateway session; every later case reuses the result."""
    key = id(gateway._session)
    cached = _TOOL_CACHE.get(key)
    if cached is None:
        response = await gateway._session.list_tools()
        cached = {tool.name: tool for tool in response.tools}
        _TOOL_CACHE.clear()
        _TOOL_CACHE[key] = cached
    return cached


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_rows(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Normalise a tool payload into a list of records."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [data]
    return []


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _safe_call(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    **kwargs: str,
) -> dict[str, Any] | None:
    try:
        evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
    except Exception as exc:  # a single unavailable domain must not lose the case
        print(f"[{tool_name} error: {exc}]", end=" ", flush=True)
        return None
    if not isinstance(evidence, dict) or "evidence_ref" not in evidence:
        print(f"[{tool_name} bad response: {evidence!r}]", end=" ", flush=True)
        return None
    ref = evidence["evidence_ref"]
    if not isinstance(ref, str) or not EV_REF_PATTERN.fullmatch(ref):
        print(f"[{tool_name} bad ref: {ref!r}]", end=" ", flush=True)
        return None
    return evidence


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id: str = case["case_id"]
    customer_request: dict[str, Any] = case.get("customer_request", {})
    claimed_order_id: str | None = customer_request.get("claimed_order_id")
    claims: list[dict[str, Any]] = customer_request.get("claims", [])
    policy_version: str = case.get("policy_version", "EC_POLICY_V1")

    discovered_tools = await _discover_tools(gateway)

    # The claim topic is only a hypothesis: the customer message is not ground truth.
    claimed_topic = "unsupported_claim"
    for claim in claims:
        topic = claim.get("topic")
        if topic in TOOLS_BY_TOPIC:
            claimed_topic = topic
            break

    refs_by_tool: dict[str, str] = {}

    def consume(evidence: dict[str, Any] | None, actor: str, tool_name: str) -> Any:
        """Record an evidence ref in the trace and return its payload."""
        if evidence is None:
            return None
        ref = evidence.get("evidence_ref")
        if ref and tool_name not in refs_by_tool:
            refs_by_tool[tool_name] = ref
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[ref],
            )
        return evidence.get("data")

    async def fetch(tool_name: str, actor: str, **kwargs: str) -> Any:
        if tool_name not in discovered_tools:
            return None
        evidence = await _safe_call(gateway, tool_name, case_id=case_id, **kwargs)
        return consume(evidence, actor, tool_name)

    # -------------------------------------------------------------------------
    # 1. COORDINATOR — plan which domains this hypothesis needs
    # -------------------------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order_specialist",
        decision_code="DISPATCH_ORDER_INVESTIGATION",
        attributes={"claimed_topic": claimed_topic},
    )

    planned = set(TOOLS_BY_TOPIC.get(claimed_topic, TOOLS_BY_TOPIC["unsupported_claim"]))

    # -------------------------------------------------------------------------
    # 2. ORDER SPECIALIST
    # -------------------------------------------------------------------------
    order_data: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    seller_rows: list[dict[str, Any]] = []

    if claimed_order_id:
        result = await fetch("get_order", "order_specialist", order_id=claimed_order_id)
        if isinstance(result, dict):
            order_data = result

        if "get_order_items" in planned:
            result = await fetch(
                "get_order_items", "order_specialist", order_id=claimed_order_id
            )
            items = _as_rows(result, "items", "order_items")

        if "get_sellers" in planned:
            result = await fetch("get_sellers", "order_specialist", order_id=claimed_order_id)
            seller_rows = _as_rows(result, "sellers")

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order_specialist",
        target="payment_specialist",
    )

    # -------------------------------------------------------------------------
    # 3. PAYMENT SPECIALIST
    # -------------------------------------------------------------------------
    payments: list[dict[str, Any]] = []
    payment_events: list[dict[str, Any]] = []
    refund_events: list[dict[str, Any]] = []

    if claimed_order_id:
        if "get_order_payments" in planned:
            result = await fetch(
                "get_order_payments", "payment_specialist", order_id=claimed_order_id
            )
            payments = _as_rows(result, "payments", "order_payments")

        if "get_payment_timeline" in planned:
            result = await fetch(
                "get_payment_timeline", "payment_specialist", order_id=claimed_order_id
            )
            if isinstance(result, dict):
                payment_events = _as_rows(result.get("events", []))
                if not payments:
                    payments = _as_rows(result.get("payments", []))

        if "get_refund_timeline" in planned:
            result = await fetch(
                "get_refund_timeline", "payment_specialist", order_id=claimed_order_id
            )
            if isinstance(result, dict):
                refund_events = _as_rows(result.get("events", []))

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment_specialist",
        target="shipment_specialist",
    )

    # -------------------------------------------------------------------------
    # 4. SHIPMENT SPECIALIST
    # -------------------------------------------------------------------------
    shipment_data: dict[str, Any] = {}
    shipment_events: list[dict[str, Any]] = []
    if claimed_order_id and "get_shipment_summary" in planned:
        result = await fetch(
            "get_shipment_summary", "shipment_specialist", order_id=claimed_order_id
        )
        if isinstance(result, dict):
            shipment_data = result
            shipment_events = _as_rows(result.get("events", []))

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment_specialist",
        target="policy_specialist",
    )

    # -------------------------------------------------------------------------
    # 5. POLICY SPECIALIST — verify the hypothesis, then apply the authoritative rule
    # -------------------------------------------------------------------------
    policy_data = await fetch("get_policy", "policy_specialist", policy_version=policy_version)
    policy_rules: dict[str, Any] = {}
    if isinstance(policy_data, dict) and isinstance(policy_data.get("rules"), dict):
        policy_rules = policy_data["rules"]

    # --- derive the facts the verification needs -----------------------------
    order_status = str(order_data.get("order_status", "")).lower()
    paid_total = round(sum(_as_float(row.get("payment_value")) for row in payments), 2)
    items_total = round(sum(_as_float(row.get("price")) for row in items), 2)
    freight_total = round(sum(_as_float(row.get("freight_value")) for row in items), 2)
    order_total = round(items_total + freight_total, 2)
    distinct_payment_count = len(
        {
            (row.get("payment_sequential"), row.get("payment_type"), row.get("payment_value"))
            for row in payments
        }
    )

    captured_amounts = [
        _as_float(event.get("amount_brl"))
        for event in payment_events
        if str(event.get("event_type", "")).lower() == "captured"
    ]
    has_duplicate_capture = len(captured_amounts) > len(set(captured_amounts))

    refund_states = {str(event.get("event_type", "")).lower() for event in refund_events}
    refund_states |= {str(event.get("status", "")).lower() for event in refund_events}

    delivered_at = _parse_time(
        shipment_data.get("delivered_customer_at") or order_data.get("order_delivered_customer_date")
    )
    estimated_at = _parse_time(
        shipment_data.get("estimated_delivery_at")
        or order_data.get("order_estimated_delivery_date")
    )
    delivered_late = bool(delivered_at and estimated_at and delivered_at > estimated_at)

    # A `delivered_late` event alone proves nothing: these cases carry planted events
    # whose event_at is months away from the real delivery. Lateness is only real when
    # the order actually arrived after the estimate; the event then names who caused it.
    late_actors: set[str] = set()
    if delivered_late:
        late_actors = {
            str(event.get("actor", "")).lower()
            for event in shipment_events
            if "late" in str(event.get("event_type", "")).lower()
        }
        late_actors.discard("")

    # --- verification: only override the claim when evidence contradicts it ---
    data_conflicts: list[dict[str, Any]] = []
    primary_issue = claimed_topic
    overridden = False

    def override(new_topic: str, field: str, source: str, code: str) -> None:
        nonlocal primary_issue, overridden
        if new_topic == primary_issue:
            return
        data_conflicts.append(
            {
                "field": field,
                "sources": ["customer_request.claims", source],
                "selected_source": source,
                "resolution_code": code,
            }
        )
        primary_issue = new_topic
        overridden = True

    if not order_data:
        primary_issue = "insufficient_evidence"
    elif claimed_topic in ("canceled_order_paid", "unavailable_order_paid"):
        expected = "canceled" if claimed_topic == "canceled_order_paid" else "unavailable"
        if order_status and order_status != expected:
            if order_status in ("canceled", "cancelled"):
                override(
                    "canceled_order_paid",
                    "assessment.primary_issue",
                    "get_order.order_status",
                    "PREFER_ORDER_STATUS_EVIDENCE",
                )
            elif order_status == "unavailable":
                override(
                    "unavailable_order_paid",
                    "assessment.primary_issue",
                    "get_order.order_status",
                    "PREFER_ORDER_STATUS_EVIDENCE",
                )
            else:
                override(
                    "unsupported_claim",
                    "assessment.primary_issue",
                    "get_order.order_status",
                    "CLAIM_CONTRADICTED_BY_ORDER_STATUS",
                )
    elif claimed_topic in ("late_delivery_seller", "late_delivery_logistics"):
        if late_actors:
            actual = (
                "late_delivery_seller" if "seller" in late_actors else "late_delivery_logistics"
            )
            override(
                actual,
                "root_cause_analysis.responsible_parties",
                "get_shipment_summary.events.actor",
                "PREFER_SHIPMENT_EVENT_ACTOR",
            )
        elif not delivered_late and delivered_at is not None:
            override(
                "unsupported_claim",
                "assessment.primary_issue",
                "get_shipment_summary.delivered_customer_at",
                "DELIVERY_WITHIN_ESTIMATE",
            )
    elif claimed_topic == "duplicate_charge":
        if payment_events and not has_duplicate_capture:
            override(
                "unsupported_claim",
                "assessment.primary_issue",
                "get_payment_timeline.events",
                "NO_DUPLICATE_CAPTURE_FOUND",
            )
    elif claimed_topic in ("refund_pending", "refund_failed"):
        if refund_states:
            if "failed" in refund_states:
                override(
                    "refund_failed",
                    "assessment.primary_issue",
                    "get_refund_timeline.events",
                    "PREFER_REFUND_TIMELINE_STATE",
                )
            elif refund_states & {"pending", "processing", "requested", "initiated"}:
                override(
                    "refund_pending",
                    "assessment.primary_issue",
                    "get_refund_timeline.events",
                    "PREFER_REFUND_TIMELINE_STATE",
                )
    elif claimed_topic == "valid_split_payment":
        if payments and distinct_payment_count < 2:
            override(
                "unsupported_claim",
                "assessment.primary_issue",
                "get_order_payments",
                "SINGLE_PAYMENT_NOT_A_SPLIT",
            )
    elif claimed_topic == "unsupported_claim":
        if order_status in ("canceled", "cancelled") and paid_total > 0:
            override(
                "canceled_order_paid",
                "assessment.primary_issue",
                "get_order.order_status",
                "EVIDENCE_SUPPORTS_STRONGER_ISSUE",
            )
        elif late_actors:
            actual = (
                "late_delivery_seller" if "seller" in late_actors else "late_delivery_logistics"
            )
            override(
                actual,
                "assessment.primary_issue",
                "get_shipment_summary.events",
                "EVIDENCE_SUPPORTS_STRONGER_ISSUE",
            )

    # --- apply the authoritative policy rule for the verified issue ----------
    rule: dict[str, Any] = {}
    if isinstance(policy_rules.get(primary_issue), dict):
        rule = policy_rules[primary_issue]

    if rule:
        case_status = str(rule.get("case_status", "needs_investigation"))
        refund_amount = round(_as_float(rule.get("refund_brl")), 2)
        responsible_parties = [
            {
                "party_type": str(party.get("party_type", "unknown")),
                "party_id": party.get("party_id"),
            }
            for party in _as_rows(rule.get("responsible_parties", []))
            if party.get("party_type")
        ]
        resolution_actions = [str(rule["recommended_action"])] if rule.get("recommended_action") else []
    else:
        # No rule for this issue (e.g. insufficient_evidence): escalate, refund nothing.
        case_status = "needs_investigation"
        refund_amount = 0.0
        responsible_parties = [{"party_type": "unknown", "party_id": None}]
        resolution_actions = ["escalate_manual_review"]

    if not responsible_parties:
        responsible_parties = [{"party_type": "unknown", "party_id": None}]

    # Name the real seller when the seller is the responsible party.
    seller_ids = [
        str(row["seller_id"])
        for row in items + seller_rows
        if row.get("seller_id")
    ]
    seller_ids = list(dict.fromkeys(seller_ids))
    for party in responsible_parties:
        if party["party_type"] == "seller" and not party["party_id"] and seller_ids:
            party["party_id"] = seller_ids[0]

    # Refund lines must sum exactly to the recommended refund.
    refund_lines: list[dict[str, Any]] = []
    if case_status == "action_required" and refund_amount > 0:
        refund_lines.append(
            {
                "reason_code": REFUND_REASON_BY_TOPIC.get(primary_issue, "REFUND_POLICY_RULE"),
                "amount_brl": refund_amount,
                "entity_id": claimed_order_id,
            }
        )
    else:
        refund_amount = 0.0

    ranked_causes = [
        {
            "cause_code": CAUSE_CODE_BY_TOPIC.get(primary_issue, "UNCLASSIFIED_ISSUE"),
            "rank": 1,
        }
    ]

    # --- calibrated confidence ----------------------------------------------
    if primary_issue == "insufficient_evidence":
        confidence = 0.3
    elif not rule:
        confidence = 0.5
    elif overridden:
        confidence = 0.8  # direct evidence, but it contradicts the customer
    elif len(refs_by_tool) >= len(planned):
        confidence = 0.93  # every planned domain confirmed the claim
    else:
        confidence = 0.75  # decision stands on partial evidence

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy_specialist",
        decision_code=f"RESOLVE_{primary_issue.upper()}"[:80],
        evidence_refs=sorted(refs_by_tool.values()),
        attributes={
            "case_status": case_status,
            "refund_brl": refund_amount,
            "overridden_claim": overridden,
        },
    )

    # --- per-claim verdicts, each citing only the evidence it rests on -------
    claim_refs: dict[str, list[str]] = {
        "order": [refs_by_tool[t] for t in ("get_order", "get_order_items") if t in refs_by_tool],
        "payment": [
            refs_by_tool[t]
            for t in ("get_order_payments", "get_payment_timeline", "get_refund_timeline")
            if t in refs_by_tool
        ],
        "shipment": [
            refs_by_tool[t]
            for t in ("get_shipment_summary", "get_sellers")
            if t in refs_by_tool
        ],
    }

    def refs_for(topic: str) -> list[str]:
        if topic in ("late_delivery_seller", "late_delivery_logistics"):
            groups = ["order", "shipment"]
        elif topic in (
            "valid_split_payment",
            "payment_mismatch",
            "duplicate_charge",
            "refund_pending",
            "refund_failed",
        ):
            groups = ["order", "payment"]
        elif topic in ("canceled_order_paid", "unavailable_order_paid"):
            groups = ["order", "payment"]
        else:
            groups = ["order", "payment", "shipment"]
        picked: list[str] = []
        for group in groups:
            picked.extend(claim_refs.get(group, []))
        policy_ref = refs_by_tool.get("get_policy")
        if policy_ref:
            picked.append(policy_ref)
        return list(dict.fromkeys(picked))[:20]

    claim_assessments: list[dict[str, Any]] = []
    for claim in claims:
        topic = claim.get("topic", "")
        if primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == primary_issue:
            verdict = "supported" if case_status != "no_action" else "unsupported"
        elif topic == "requested_full_refund":
            if refund_amount <= 0:
                verdict = "unsupported"
            elif order_total > 0 and refund_amount + 0.01 < order_total:
                verdict = "partially_supported"
            else:
                verdict = "supported"
        elif topic == "unsupported_claim":
            verdict = "unsupported"
        elif case_status == "action_required":
            verdict = "partially_supported"
        else:
            verdict = "unsupported"

        claim_assessments.append(
            {
                "claim_id": claim.get("claim_id", ""),
                "verdict": verdict,
                "confidence": round(confidence, 2),
                "evidence_refs": refs_for(topic or primary_issue),
            }
        )

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy_specialist",
        target="verifier",
    )

    # -------------------------------------------------------------------------
    # 6. VERIFIER — cross-field invariants before the output leaves the agent
    # -------------------------------------------------------------------------
    payment_references = list(
        dict.fromkeys(
            f"{row.get('payment_type', 'payment')}-{row.get('payment_sequential', '0')}"
            f"-{_as_float(row.get('payment_value')):.2f}"
            for row in payments
        )
    )
    item_ids = list(
        dict.fromkeys(str(row["order_item_id"]) for row in items if row.get("order_item_id"))
    )
    shipment_id = shipment_data.get("shipment_id") or order_data.get("shipment_id")

    line_total = round(sum(line["amount_brl"] for line in refund_lines), 2)
    if line_total != refund_amount:
        refund_amount = line_total
    if case_status != "action_required" and refund_amount > 0:
        refund_amount = 0.0
        refund_lines = []

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="INVARIANTS_SATISFIED",
        attributes={
            "evidence_count": len(refs_by_tool),
            "conflicts": len(data_conflicts),
        },
    )

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": round(confidence, 2),
        },
        "affected_entities": {
            "order_ids": [claimed_order_id] if claimed_order_id else [],
            "item_ids": item_ids[:20],
            "seller_ids": seller_ids[:20],
            "payment_references": payment_references[:20],
            "shipment_ids": [str(shipment_id)] if shipment_id else [],
        },
        "claim_assessments": claim_assessments[:5],
        "root_cause_analysis": {
            "ranked_causes": ranked_causes[:5],
            "responsible_parties": responsible_parties[:5],
        },
        "evidence_refs": sorted(refs_by_tool.values())[:30],
        "data_conflicts": data_conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": refund_lines[:10],
        },
        "resolution_actions": list(dict.fromkeys(resolution_actions))[:8],
    }
