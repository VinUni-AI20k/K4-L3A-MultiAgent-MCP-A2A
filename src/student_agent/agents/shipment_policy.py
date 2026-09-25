"""Shipment & Policy Agent — Specialist #4.

Investigates delivery timelines and cross-references platform policy to:
- determine whether a delivery was late,
- attribute responsibility (seller vs logistics provider),
- look up the applicable refund/action from the policy rulebook,
- produce a structured report for the Verifier agent.

MCP tools granted:
    get_shipment_summary  — delivery timestamps, carrier handoff, events
    get_policy            — machine-readable policy rules per issue type
    get_sellers           — seller records for the order
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from student_agent.mcp_gateway import EvidenceGateway
from student_agent.trace import TraceWriter
from student_agent.verifier import detect_late_delivery, scope_facts

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACTOR = "shipment_policy_agent"
MAX_RETRIES = 1
RETRY_BASE_DELAY = 1.0  # seconds, doubles on each retry

# Topics this agent specialises in
SHIPMENT_TOPICS = frozenset({
    "late_delivery_seller",
    "late_delivery_logistics",
})

# All primary issue codes the policy may reference
ALL_PRIMARY_ISSUES = frozenset({
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
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string; return None on missing/invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


async def _call_with_retry(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    max_retries: int = MAX_RETRIES,
    **kwargs: str,
) -> dict[str, Any] | None:
    """Call an MCP tool with exponential-backoff retry on transient errors.

    Returns None when all retries are exhausted (data_unavailable fallback).
    """
    delay = RETRY_BASE_DELAY
    for attempt in range(1, max_retries + 1):
        try:
            return await gateway.call(tool_name, case_id=case_id, **kwargs)
        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc).lower()
            # Non-retryable: 404 / not found
            if "not found" in error_msg or "404" in error_msg:
                logger.warning(
                    "[%s] %s returned not-found for case=%s (attempt %d)",
                    ACTOR, tool_name, case_id, attempt,
                )
                return None
            # Retryable: timeouts, 5xx, connection errors, etc.
            if attempt < max_retries:
                logger.warning(
                    "[%s] %s failed (attempt %d/%d): %s — retrying in %.1fs",
                    ACTOR, tool_name, attempt, max_retries, exc, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    "[%s] %s exhausted retries for case=%s: %s",
                    ACTOR, tool_name, case_id, exc,
                )
                return None
    return None  # unreachable, but keeps mypy happy


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def _analyse_shipment(
    shipment_data: dict[str, Any],
    seller_ids: list[str],
) -> dict[str, Any]:
    """Core business logic: determine delivery issue and responsible party.

    Returns a dict with keys:
        primary_issue, responsible_parties, ranked_causes,
        is_late, seller_delayed, logistics_delayed, order_status
    """
    delivered_carrier = _parse_dt(shipment_data.get("delivered_carrier_at"))
    delivered_customer = _parse_dt(shipment_data.get("delivered_customer_at"))
    estimated_delivery = _parse_dt(shipment_data.get("estimated_delivery_at"))
    shipping_limits = shipment_data.get("shipping_limits", [])
    events = shipment_data.get("events", [])
    order_status = shipment_data.get("order_status", "unknown")

    # --- Check if seller handed off to carrier late ---
    seller_delayed = False
    seller_delayed_ids: set[str] = set()
    for limit in shipping_limits:
        limit_at = _parse_dt(limit.get("shipping_limit_at"))
        if delivered_carrier and limit_at and delivered_carrier > limit_at:
            seller_delayed = True
            sid = limit.get("seller_id")
            if sid:
                seller_delayed_ids.add(sid)

    # Check event-level signals
    for evt in events:
        if evt.get("event_type") == "delivered_late" and evt.get("actor") == "seller":
            seller_delayed = True
            # Try to find the seller from shipping_limits
            for limit in shipping_limits:
                sid = limit.get("seller_id")
                if sid:
                    seller_delayed_ids.add(sid)

    # --- Check if customer received late ---
    logistics_delayed = False
    if delivered_customer and estimated_delivery and delivered_customer > estimated_delivery:
        logistics_delayed = True
    # Also treat never-delivered as a potential logistics issue
    if delivered_customer is None and estimated_delivery and order_status not in ("canceled",):
        logistics_delayed = True

    # Check for logistics events
    for evt in events:
        if evt.get("event_type") == "delivered_late" and evt.get("actor") in (
            "logistics",
            "carrier",
            "logistics_provider",
        ):
            logistics_delayed = True

    # --- Determine primary issue ---
    ranked_causes: list[dict[str, Any]] = []
    responsible_parties: list[dict[str, Any]] = []

    if seller_delayed:
        primary_issue = "late_delivery_seller"
        ranked_causes.append({"cause_code": "LATE_DELIVERY_SELLER", "rank": 1})
        # Use the sellers that were specifically late, or all sellers
        late_ids = seller_delayed_ids or set(seller_ids)
        for sid in sorted(late_ids):
            responsible_parties.append({"party_type": "seller", "party_id": sid})
        if logistics_delayed:
            ranked_causes.append({"cause_code": "LATE_DELIVERY_LOGISTICS", "rank": 2})
    elif logistics_delayed:
        primary_issue = "late_delivery_logistics"
        ranked_causes.append({"cause_code": "LATE_DELIVERY_LOGISTICS", "rank": 1})
        responsible_parties.append({"party_type": "logistics_provider", "party_id": None})
    else:
        # No delivery issue detected
        primary_issue = "unsupported_claim"
        ranked_causes.append({"cause_code": "NO_DELIVERY_ISSUE", "rank": 1})
        responsible_parties.append({"party_type": "customer", "party_id": None})

    return {
        "primary_issue": primary_issue,
        "responsible_parties": responsible_parties,
        "ranked_causes": ranked_causes,
        "is_late": seller_delayed or logistics_delayed,
        "seller_delayed": seller_delayed,
        "logistics_delayed": logistics_delayed,
        "order_status": order_status,
    }


def _analysis_from_order(
    order: dict[str, Any],
    opened_at: str | None,
    shipment_data: dict[str, Any],
    sellers_data: list[dict[str, Any]],
) -> dict[str, Any]:
    """Same result shape as _analyse_shipment, but scoped to the authoritative order row.

    Lateness comes from the order timestamps; stale ``delivered_late`` events and
    shipping limits outside the order lifecycle (distractor rows) are ignored.
    """
    limits = [
        {
            "order_item_id": limit.get("order_item_id"),
            "seller_id": limit.get("seller_id"),
            "shipping_limit_date": limit.get("shipping_limit_at"),
        }
        for limit in shipment_data.get("shipping_limits", [])
    ]
    facts = scope_facts(
        {"opened_at": opened_at},
        {
            "get_order": order,
            "get_order_items": limits,
            "get_shipment_summary": shipment_data,
            "get_sellers": sellers_data,
        },
    )
    decision = detect_late_delivery(facts)
    order_status = order.get("order_status", "unknown")
    if decision is None:
        return {
            "primary_issue": "unsupported_claim",
            "responsible_parties": [{"party_type": "customer", "party_id": None}],
            "ranked_causes": [{"cause_code": "NO_DELIVERY_ISSUE", "rank": 1}],
            "is_late": False,
            "seller_delayed": False,
            "logistics_delayed": False,
            "order_status": order_status,
        }
    seller_late = decision.issue == "late_delivery_seller"
    parties = (
        [{"party_type": "seller", "party_id": sid} for sid in facts.seller_ids]
        if seller_late
        else [{"party_type": "logistics_provider", "party_id": None}]
    )
    return {
        "primary_issue": decision.issue,
        "responsible_parties": parties or [{"party_type": "seller", "party_id": None}],
        "ranked_causes": [{"cause_code": decision.issue.upper(), "rank": 1}],
        "is_late": True,
        "seller_delayed": seller_late,
        "logistics_delayed": not seller_late,
        "order_status": order_status,
    }


def _assess_claim(
    claim: dict[str, Any],
    shipment_analysis: dict[str, Any],
    policy_rules: dict[str, Any],
    evidence_refs: list[str],
) -> dict[str, Any]:
    """Evaluate one customer claim against evidence and policy.

    Returns a claim_assessment dict matching the output schema.
    """
    topic = claim.get("topic", "")
    claim_id = claim.get("claim_id", "unknown")

    # Claims we can directly adjudicate
    if topic in ("late_delivery_seller", "late_delivery_logistics"):
        actual_issue = shipment_analysis["primary_issue"]
        if actual_issue == topic:
            verdict = "supported"
            confidence = 0.95
        elif actual_issue in SHIPMENT_TOPICS:
            # Customer claimed the wrong party, but there IS a late delivery
            verdict = "partially_supported"
            confidence = 0.70
        elif shipment_analysis["is_late"]:
            verdict = "partially_supported"
            confidence = 0.65
        else:
            verdict = "unsupported"
            confidence = 0.90
    elif topic == "requested_full_refund":
        # Refund entitlement depends on whether there is a real issue
        if shipment_analysis["is_late"]:
            verdict = "partially_supported"
            confidence = 0.75
        elif shipment_analysis["order_status"] == "canceled":
            verdict = "partially_supported"
            confidence = 0.70
        else:
            verdict = "unsupported"
            confidence = 0.80
    elif topic in ALL_PRIMARY_ISSUES:
        # Not our specialty — defer with insufficient_evidence
        verdict = "insufficient_evidence"
        confidence = 0.40
    else:
        verdict = "insufficient_evidence"
        confidence = 0.35

    return {
        "claim_id": claim_id,
        "verdict": verdict,
        "confidence": confidence,
        "evidence_refs": list(evidence_refs),
    }


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------

class ShipmentPolicyAgent:
    """Specialist agent that investigates shipment timelines and applies policy rules.

    Workflow:
        1. Fetch shipment summary via MCP
        2. Fetch seller information via MCP
        3. Fetch applicable policy via MCP
        4. Analyse timeline to determine late-delivery responsibility
        5. Cross-reference policy for recommended action & refund amount
        6. Assess each customer claim
        7. Return structured report for the Verifier
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    # ---- public interface ----

    async def investigate(
        self,
        *,
        case_id: str,
        order_id: str,
        claims: list[dict[str, Any]],
        policy_version: str = "EC_POLICY_V1",
        order: dict[str, Any] | None = None,
        opened_at: str | None = None,
    ) -> dict[str, Any]:
        """Run the full shipment & policy investigation for a single case.

        Parameters
        ----------
        case_id : str
            The L3A case identifier (e.g. ``"L3A_CASE_001"``).
        order_id : str
            The Olist order identifier from the customer's claim.
        claims : list[dict]
            Customer claims, each with ``claim_id`` and ``topic``.
        policy_version : str
            Policy document version (usually ``"EC_POLICY_V1"``).
        order : dict, optional
            Authoritative order row from the Order agent. When given, lateness is judged
            on the order lifecycle and distractor rows are ignored.
        opened_at : str, optional
            Case opening timestamp, used to scope events to the order lifecycle.

        Returns
        -------
        dict
            Structured report containing evidence_refs, claim_assessments,
            root_cause analysis, entity IDs, and policy recommendation.
        """
        collected_evidence_refs: list[str] = []
        data_conflicts: list[dict[str, Any]] = []

        # ── 1. Fetch shipment summary ──────────────────────────────
        shipment_ev = await _call_with_retry(
            self.gateway,
            "get_shipment_summary",
            case_id=case_id,
            order_id=order_id,
        )
        if shipment_ev is not None:
            shipment_ref = shipment_ev["evidence_ref"]
            collected_evidence_refs.append(shipment_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=ACTOR,
                tool_name="get_shipment_summary",
                evidence_refs=[shipment_ref],
            )
            shipment_data = shipment_ev.get("data", {})
        else:
            shipment_ref = None
            shipment_data = {}
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=ACTOR,
                tool_name="get_shipment_summary",
                attributes={"status": "data_unavailable"},
            )

        # ── 2. Fetch seller information ────────────────────────────
        seller_ev = await _call_with_retry(
            self.gateway,
            "get_sellers",
            case_id=case_id,
            order_id=order_id,
        )
        if seller_ev is not None:
            seller_ref = seller_ev["evidence_ref"]
            collected_evidence_refs.append(seller_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=ACTOR,
                tool_name="get_sellers",
                evidence_refs=[seller_ref],
            )
            sellers_data = seller_ev.get("data", [])
        else:
            seller_ref = None
            sellers_data = []
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=ACTOR,
                tool_name="get_sellers",
                attributes={"status": "data_unavailable"},
            )

        seller_ids = [s["seller_id"] for s in sellers_data if "seller_id" in s]

        # ── 3. Fetch policy ────────────────────────────────────────
        policy_ev = await _call_with_retry(
            self.gateway,
            "get_policy",
            case_id=case_id,
            policy_version=policy_version,
        )
        if policy_ev is not None:
            policy_ref = policy_ev["evidence_ref"]
            collected_evidence_refs.append(policy_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=ACTOR,
                tool_name="get_policy",
                evidence_refs=[policy_ref],
            )
            policy_data = policy_ev.get("data", {})
        else:
            policy_ref = None
            policy_data = {}
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=ACTOR,
                tool_name="get_policy",
                attributes={"status": "data_unavailable"},
            )

        policy_rules = policy_data.get("rules", {})

        # ── 4. Analyse shipment timeline ───────────────────────────
        if order:
            analysis = _analysis_from_order(order, opened_at, shipment_data, sellers_data)
        else:
            analysis = _analyse_shipment(shipment_data, seller_ids)

        # Cross-check policy-listed responsible parties vs our analysis
        primary_issue = analysis["primary_issue"]
        policy_rule = policy_rules.get(primary_issue, {})
        policy_parties = policy_rule.get("responsible_parties", [])

        # Detect conflict if policy says different responsible party
        if policy_parties and analysis["responsible_parties"]:
            policy_party_types = {p.get("party_type") for p in policy_parties}
            our_party_types = {p.get("party_type") for p in analysis["responsible_parties"]}
            if policy_party_types != our_party_types:
                data_conflicts.append({
                    "field": "responsible_party_type",
                    "sources": sorted([
                        f"shipment_analysis:{','.join(sorted(our_party_types))}",
                        f"policy_rule:{','.join(sorted(policy_party_types))}",
                    ]),
                    "selected_source": "shipment_analysis",
                    "resolution_code": "evidence_takes_precedence",
                })

        # Emit the policy decision trace event
        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=ACTOR,
            decision_code=primary_issue.upper(),
            evidence_refs=[
                ref for ref in [shipment_ref, policy_ref] if ref is not None
            ],
            attributes={
                "primary_issue": primary_issue,
                "case_status": policy_rule.get("case_status", "needs_investigation"),
                "refund_brl": policy_rule.get("refund_brl", 0.0),
            },
        )

        # ── 5. Assess each customer claim ──────────────────────────
        # Build the evidence refs to cite in each claim assessment
        claim_evidence = [ref for ref in [shipment_ref, policy_ref] if ref is not None]
        claim_assessments = [
            _assess_claim(claim, analysis, policy_rules, claim_evidence)
            for claim in claims
        ]

        # ── 6. Derive resolution from policy ───────────────────────
        case_status = policy_rule.get("case_status", "needs_investigation")
        recommended_action = policy_rule.get("recommended_action", "document_no_action")
        refund_brl = policy_rule.get("refund_brl", 0.0)

        # Build responsible_parties — prefer policy's party_id when our analysis
        # found the issue type but didn't have a specific ID
        final_parties = list(analysis["responsible_parties"])
        if policy_parties:
            for i, party in enumerate(final_parties):
                if party.get("party_id") is None:
                    # Look for a matching policy party with a concrete ID
                    for pp in policy_parties:
                        if pp.get("party_type") == party.get("party_type") and pp.get("party_id"):
                            final_parties[i] = dict(party, party_id=pp["party_id"])
                            break

        # ── 7. Emit handoff trace ──────────────────────────────────
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=ACTOR,
            target="verifier_agent",
            attributes={
                "evidence_count": len(collected_evidence_refs),
                "primary_issue": primary_issue,
            },
        )

        # ── Build the report ───────────────────────────────────────
        return {
            "agent": ACTOR,
            "case_id": case_id,
            "evidence_refs": collected_evidence_refs,
            "claim_assessments": claim_assessments,
            "root_cause": {
                "ranked_causes": analysis["ranked_causes"],
                "responsible_parties": final_parties,
            },
            "entities": {
                "seller_ids": seller_ids,
                "shipment_ids": [order_id],
            },
            "policy_recommendation": {
                "primary_issue": primary_issue,
                "case_status": case_status,
                "recommended_action": recommended_action,
                "refund_brl": refund_brl,
            },
            "data_conflicts": data_conflicts,
        }
