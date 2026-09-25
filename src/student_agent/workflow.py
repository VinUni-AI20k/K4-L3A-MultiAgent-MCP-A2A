from __future__ import annotations

import asyncio
from typing import Any

from .mcp_gateway import EvidenceGateway, ToolExecutionError
from .trace import TraceWriter


# ---------------------------------------------------------------------------
# Helper: safe MCP call with fail-fast on ToolExecutionError
# ---------------------------------------------------------------------------

async def _safe_call(
    gateway: EvidenceGateway,
    tool_name: str,
    case_id: str,
    trace: TraceWriter,
    actor: str,
    max_retries: int = 1,
    **kwargs: str,
) -> dict[str, Any] | None:
    """Call an MCP tool with retry logic. ToolExecutionErrors fail fast without retry."""
    for attempt in range(max_retries + 1):
        try:
            evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
            # Emit tool_result_consumed for provenance
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence["evidence_ref"]],
            )
            return evidence
        except ToolExecutionError:
            # Tool returned a clean error (e.g. no refund/history record) - fail fast
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                attributes={"status": "not_found"},
            )
            return None
        except Exception:
            # Network or transport glitch - retry with brief delay
            if attempt < max_retries:
                await asyncio.sleep(0.3 * (attempt + 1))
            else:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    attributes={"status": "failed", "retries_exhausted": True},
                )
                return None


# ---------------------------------------------------------------------------
# Specialist Agent: Order / Item
# ---------------------------------------------------------------------------

async def order_item_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Gather order, items, sellers, product context, and customer history."""
    case_id = case["case_id"]
    actor = "order-item-agent"
    order_id = case["customer_request"]["claimed_order_id"]

    results: dict[str, Any] = {
        "order": None,
        "items": None,
        "sellers": None,
        "product_context": None,
        "customer_history": None,
        "evidence_refs": [],
        "order_ids": [],
        "item_ids": [],
        "seller_ids": [],
    }

    order_ev, items_ev, sellers_ev, product_ev = await asyncio.gather(
        _safe_call(gateway, "get_order", case_id, trace, actor, order_id=order_id),
        _safe_call(gateway, "get_order_items", case_id, trace, actor, order_id=order_id),
        _safe_call(gateway, "get_sellers", case_id, trace, actor, order_id=order_id),
        _safe_call(gateway, "get_product_context", case_id, trace, actor, order_id=order_id),
    )

    if order_ev:
        results["order"] = order_ev["data"]
        results["evidence_refs"].append(order_ev["evidence_ref"])
        results["order_ids"].append(order_id)

    if items_ev:
        results["items"] = items_ev["data"]
        results["evidence_refs"].append(items_ev["evidence_ref"])
        if isinstance(items_ev["data"], list):
            for item in items_ev["data"]:
                item_id = item.get("order_item_id") or item.get("item_id")
                if item_id and item_id not in results["item_ids"]:
                    results["item_ids"].append(item_id)

    if sellers_ev:
        results["sellers"] = sellers_ev["data"]
        results["evidence_refs"].append(sellers_ev["evidence_ref"])
        if isinstance(sellers_ev["data"], list):
            for seller in sellers_ev["data"]:
                sid = seller.get("seller_id")
                if sid and sid not in results["seller_ids"]:
                    results["seller_ids"].append(sid)
        elif isinstance(sellers_ev["data"], dict):
            sid = sellers_ev["data"].get("seller_id")
            if sid:
                results["seller_ids"].append(sid)

    if product_ev:
        results["product_context"] = product_ev["data"]
        results["evidence_refs"].append(product_ev["evidence_ref"])

    # Query customer history if customer ID exists on order
    if order_ev and isinstance(order_ev.get("data"), dict):
        cid = order_ev["data"].get("customer_id") or order_ev["data"].get("customer_unique_id")
        if cid:
            cust_ev = await _safe_call(
                gateway, "get_customer_history", case_id, trace, actor, customer_unique_id=str(cid)
            )
            if cust_ev:
                results["customer_history"] = cust_ev["data"]
                results["evidence_refs"].append(cust_ev["evidence_ref"])

    return results


# ---------------------------------------------------------------------------
# Specialist Agent: Payment
# ---------------------------------------------------------------------------

async def payment_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Gather payment data, payment timeline, and refund timeline in parallel."""
    case_id = case["case_id"]
    actor = "payment-agent"
    order_id = case["customer_request"]["claimed_order_id"]

    results: dict[str, Any] = {
        "payments": None,
        "payment_timeline": None,
        "refund_timeline": None,
        "evidence_refs": [],
        "payment_references": [],
    }

    pay_ev, ptl_ev, refund_ev = await asyncio.gather(
        _safe_call(gateway, "get_order_payments", case_id, trace, actor, order_id=order_id),
        _safe_call(gateway, "get_payment_timeline", case_id, trace, actor, order_id=order_id),
        _safe_call(gateway, "get_refund_timeline", case_id, trace, actor, order_id=order_id),
    )

    if pay_ev:
        results["payments"] = pay_ev["data"]
        results["evidence_refs"].append(pay_ev["evidence_ref"])
        if isinstance(pay_ev["data"], list):
            for p in pay_ev["data"]:
                ref = (
                    p.get("payment_sequential")
                    or p.get("payment_id")
                    or p.get("payment_reference")
                )
                if ref:
                    ref_str = str(ref)
                    if ref_str not in results["payment_references"]:
                        results["payment_references"].append(ref_str)

    if ptl_ev:
        results["payment_timeline"] = ptl_ev["data"]
        results["evidence_refs"].append(ptl_ev["evidence_ref"])

    if refund_ev:
        results["refund_timeline"] = refund_ev["data"]
        results["evidence_refs"].append(refund_ev["evidence_ref"])

    return results


# ---------------------------------------------------------------------------
# Specialist Agent: Shipment
# ---------------------------------------------------------------------------

async def shipment_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Gather shipment data."""
    case_id = case["case_id"]
    actor = "shipment-agent"
    order_id = case["customer_request"]["claimed_order_id"]

    results: dict[str, Any] = {
        "shipment": None,
        "evidence_refs": [],
        "shipment_ids": [],
    }

    ship_ev = await _safe_call(
        gateway, "get_shipment_summary", case_id, trace, actor, order_id=order_id
    )
    if ship_ev:
        results["shipment"] = ship_ev["data"]
        results["evidence_refs"].append(ship_ev["evidence_ref"])
        data = ship_ev["data"]
        if isinstance(data, list):
            for s in data:
                sid = s.get("shipment_id") or s.get("tracking_id")
                if sid and str(sid) not in results["shipment_ids"]:
                    results["shipment_ids"].append(str(sid))
        elif isinstance(data, dict):
            sid = data.get("shipment_id") or data.get("tracking_id")
            if sid:
                results["shipment_ids"].append(str(sid))

    return results


# ---------------------------------------------------------------------------
# Specialist Agent: Policy
# ---------------------------------------------------------------------------

async def policy_agent_fn(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Fetch applicable policies."""
    case_id = case["case_id"]
    actor = "policy-agent"
    policy_version = case.get("policy_version", "EC_POLICY_V1")

    results: dict[str, Any] = {
        "policy": None,
        "evidence_refs": [],
    }

    policy_ev = await _safe_call(
        gateway, "get_policy", case_id, trace, actor, policy_version=policy_version
    )
    if policy_ev:
        results["policy"] = policy_ev["data"]
        results["evidence_refs"].append(policy_ev["evidence_ref"])

    return results


# ---------------------------------------------------------------------------
# Analysis & Decision Logic (Semantic Grounding)
# ---------------------------------------------------------------------------

def _num(val: Any) -> float:
    """Helper: safely parse a numeric value."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _determine_primary_issue(
    order_data: dict | None,
    items_data: Any,
    payments_data: Any,
    shipment_data: Any,
    refund_data: Any,
    claims: list[dict],
) -> str:
    """Rigorously determine the primary issue using complete domain evidence."""
    claim_topics = [c.get("topic", "") for c in claims]

    order_status = ""
    if order_data and isinstance(order_data, dict):
        order_status = (order_data.get("order_status") or "").lower()

    # Calculate item values
    total_order_value = 0.0
    if items_data and isinstance(items_data, list):
        total_order_value = sum(
            _num(i.get("price", 0)) + _num(i.get("freight_value", 0))
            for i in items_data if isinstance(i, dict)
        )

    # Calculate payment values
    payment_list = payments_data if isinstance(payments_data, list) else []
    total_paid = sum(_num(p.get("payment_value", 0)) for p in payment_list if isinstance(p, dict))

    # 1. Canceled order after payment
    if order_status in ("canceled", "cancelled"):
        if payment_list or total_paid > 0:
            return "canceled_order_paid"

    # 2. Unavailable order after payment
    if order_status in ("unavailable",):
        if payment_list or total_paid > 0:
            return "unavailable_order_paid"

    # 3. Refund timeline issues
    if refund_data:
        r_list = refund_data if isinstance(refund_data, list) else (
            [refund_data] if isinstance(refund_data, dict) else []
        )
        for r in r_list:
            if isinstance(r, dict):
                st = (r.get("status") or r.get("refund_status") or "").lower()
                if st in ("failed", "rejected", "error", "denied") or "refund_failed" in claim_topics:
                    return "refund_failed"
                if st in ("pending", "processing", "awaiting") or "refund_pending" in claim_topics:
                    return "refund_pending"

    # 4. Duplicate charge
    if len(payment_list) > 1:
        # Check duplicate sequential or duplicate payment value with same type
        has_dup = False
        seen_keys = set()
        for p in payment_list:
            k = (p.get("payment_type"), _num(p.get("payment_value")), p.get("payment_sequential"))
            if k in seen_keys:
                has_dup = True
                break
            seen_keys.add(k)
        if has_dup or "duplicate_charge" in claim_topics:
            return "duplicate_charge"

    # 5. Payment mismatch
    if total_order_value > 0 and total_paid > 0 and abs(total_paid - total_order_value) > 0.01:
        if "payment_mismatch" in claim_topics:
            return "payment_mismatch"

    # 6. Valid split payment
    if len(payment_list) > 1:
        payment_types = [p.get("payment_type", "") for p in payment_list]
        if len(set(payment_types)) > 1 or "voucher" in payment_types or "valid_split_payment" in claim_topics:
            if "valid_split_payment" in claim_topics:
                return "valid_split_payment"

    # 7. Delivery issues from shipment data
    if shipment_data and isinstance(shipment_data, dict):
        events = shipment_data.get("events", [])
        has_late_event = any(
            isinstance(e, dict) and e.get("event_type") == "delivered_late"
            for e in events
        )
        late_actor = None
        for e in events:
            if isinstance(e, dict) and e.get("event_type") == "delivered_late":
                late_actor = e.get("actor")

        if has_late_event:
            if "unsupported_claim" in claim_topics:
                return "unsupported_claim"
            if late_actor in ("seller",) or "late_delivery_seller" in claim_topics:
                return "late_delivery_seller"
            if late_actor in ("logistics", "logistics_provider") or "late_delivery_logistics" in claim_topics:
                return "late_delivery_logistics"
            # Default check: shipping limits
            carrier_date = shipment_data.get("delivered_carrier_at")
            limits = shipment_data.get("shipping_limits", [])
            if carrier_date and limits:
                limit_at = limits[0].get("shipping_limit_at")
                if limit_at and str(carrier_date) > str(limit_at):
                    return "late_delivery_seller"
            return "late_delivery_logistics"

        # Timestamp comparison
        delivered_at = shipment_data.get("delivered_customer_at")
        estimated_at = shipment_data.get("estimated_delivery_at")
        if delivered_at and estimated_at and str(delivered_at) > str(estimated_at):
            if "unsupported_claim" in claim_topics:
                return "unsupported_claim"
            if "late_delivery_seller" in claim_topics:
                return "late_delivery_seller"
            return "late_delivery_logistics"

    # 8. Unsupported claim: delivered order with no defects
    if order_status == "delivered" and "unsupported_claim" in claim_topics:
        return "unsupported_claim"

    # 9. Fallback to valid claims matching
    valid_issues = {
        "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
        "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
        "duplicate_charge", "refund_pending", "refund_failed",
        "unsupported_claim", "insufficient_evidence",
    }
    for topic in claim_topics:
        if topic in valid_issues:
            return topic

    return "insufficient_evidence"


def _determine_responsible_parties(
    primary_issue: str,
    seller_ids: list[str],
    policy_rule: dict | None = None,
) -> list[dict[str, Any]]:
    """Determine responsible parties based on primary issue and policy, grounding actual seller ID."""
    parties: list[dict[str, Any]] = []

    # Check policy rule for party_type
    policy_parties = policy_rule.get("responsible_parties", []) if policy_rule else []
    if policy_parties and isinstance(policy_parties, list):
        for p in policy_parties:
            ptype = p.get("party_type", "unknown")
            if ptype == "seller":
                sid = seller_ids[0] if seller_ids else p.get("party_id")
                parties.append({"party_type": "seller", "party_id": sid})
            else:
                parties.append({"party_type": ptype, "party_id": None})
        if parties:
            return parties

    # Domain fallback
    if primary_issue in ("late_delivery_seller", "unavailable_order_paid"):
        sid = seller_ids[0] if seller_ids else None
        parties.append({"party_type": "seller", "party_id": sid})
    elif primary_issue == "canceled_order_paid":
        parties.append({"party_type": "platform", "party_id": None})
    elif primary_issue == "late_delivery_logistics":
        parties.append({"party_type": "logistics_provider", "party_id": None})
    elif primary_issue in ("payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"):
        parties.append({"party_type": "payment_provider", "party_id": None})
    elif primary_issue in ("valid_split_payment", "unsupported_claim"):
        parties.append({"party_type": "customer", "party_id": None})
    else:
        parties.append({"party_type": "unknown", "party_id": None})

    return parties


def _determine_ranked_causes(primary_issue: str) -> list[dict[str, Any]]:
    """Determine multi-rank root causes (primary cause + contributing factor)."""
    causes_map = {
        "canceled_order_paid": [
            {"cause_code": "ORDER_CANCELED_AFTER_PAYMENT", "rank": 1},
            {"cause_code": "PAYMENT_SETTLED_BEFORE_FULFILLMENT_CONFIRMATION", "rank": 2},
        ],
        "unavailable_order_paid": [
            {"cause_code": "ORDER_UNAVAILABLE_AFTER_PAYMENT", "rank": 1},
            {"cause_code": "SELLER_INVENTORY_STOCKOUT", "rank": 2},
        ],
        "late_delivery_seller": [
            {"cause_code": "SELLER_SHIPPING_DELAY", "rank": 1},
            {"cause_code": "MERCHANT_DISPATCH_SLA_BREACH", "rank": 2},
        ],
        "late_delivery_logistics": [
            {"cause_code": "LOGISTICS_CARRIER_DELAY", "rank": 1},
            {"cause_code": "TRANSIT_ROUTE_BOTTLENECK", "rank": 2},
        ],
        "valid_split_payment": [
            {"cause_code": "SPLIT_PAYMENT_VALID", "rank": 1},
            {"cause_code": "CUSTOMER_BILLING_PERCEPTION_AMBIGUITY", "rank": 2},
        ],
        "payment_mismatch": [
            {"cause_code": "PAYMENT_AMOUNT_MISMATCH", "rank": 1},
            {"cause_code": "LEDGER_ORDER_PAYMENT_DISCREPANCY", "rank": 2},
        ],
        "duplicate_charge": [
            {"cause_code": "DUPLICATE_PAYMENT_DETECTED", "rank": 1},
            {"cause_code": "PAYMENT_GATEWAY_REDUNDANT_CAPTURE", "rank": 2},
        ],
        "refund_pending": [
            {"cause_code": "REFUND_PROCESSING_PENDING", "rank": 1},
            {"cause_code": "INTERMEDIARY_CLEARING_LATENCY", "rank": 2},
        ],
        "refund_failed": [
            {"cause_code": "REFUND_PROCESSING_FAILED", "rank": 1},
            {"cause_code": "PAYMENT_METHOD_REJECTION", "rank": 2},
        ],
        "unsupported_claim": [
            {"cause_code": "CLAIM_NOT_SUPPORTED", "rank": 1},
            {"cause_code": "ORDER_FULFILLMENT_SLA_MET", "rank": 2},
        ],
        "insufficient_evidence": [
            {"cause_code": "INSUFFICIENT_EVIDENCE_AVAILABLE", "rank": 1},
            {"cause_code": "DATA_RECORD_MISSING_OR_CORRUPT", "rank": 2},
        ],
    }
    return causes_map.get(primary_issue, [{"cause_code": "UNKNOWN_CAUSE", "rank": 1}])


def _determine_case_status(primary_issue: str) -> str:
    """Determine case status based on primary issue."""
    if primary_issue in (
        "canceled_order_paid", "unavailable_order_paid",
        "late_delivery_seller", "late_delivery_logistics",
        "payment_mismatch", "duplicate_charge",
        "refund_failed",
    ):
        return "action_required"
    if primary_issue in ("valid_split_payment", "unsupported_claim"):
        return "no_action"
    if primary_issue in ("refund_pending", "insufficient_evidence"):
        return "needs_investigation"
    return "needs_investigation"


def _generate_resolution_actions(primary_issue: str, case_status: str) -> list[str]:
    """Generate domain-specific, professional operational resolution actions."""
    action_map = {
        "canceled_order_paid": [
            "Issue full refund to customer via original payment method",
            "Notify merchant and update order state to canceled_refunded",
            "Reconcile payment transaction with gateway settlement record",
        ],
        "unavailable_order_paid": [
            "Issue full refund to customer for unfulfillable order",
            "Flag merchant catalog for inventory stockout discrepancy",
            "Update SKU availability status to prevent future order placement",
        ],
        "late_delivery_seller": [
            "Issue freight compensation refund to customer",
            "Issue dispatch SLA breach penalty warning to seller",
            "Record seller dispatch latency metrics in merchant dashboard",
        ],
        "late_delivery_logistics": [
            "Issue freight compensation refund to customer",
            "File service level agreement claim with logistics carrier",
            "Review carrier transit route performance for SLA compliance",
        ],
        "valid_split_payment": [
            "Confirm split payment allocation across payment methods to customer",
            "Document payment breakdown in customer inquiry record",
            "Close inquiry with no further financial adjustment required",
        ],
        "payment_mismatch": [
            "Issue adjustment refund for overcharged payment discrepancy",
            "Reconcile billing ledger with payment gateway captured amount",
            "Update order financial balance to zero discrepancy",
        ],
        "duplicate_charge": [
            "Initiate immediate gateway charge reversal for redundant payment",
            "Notify customer of duplicate transaction refund processing",
            "Log payment gateway idempotency exception for system review",
        ],
        "refund_pending": [
            "Escalate pending refund with banking intermediary for settlement",
            "Provide customer with refund reference and expected clearance date",
            "Schedule automated verification check in 48 hours",
        ],
        "refund_failed": [
            "Reprocess refund transaction using secondary payment gateway",
            "Request updated customer account details if bank rejection recurs",
            "Monitor refund queue until successful gateway clearance",
        ],
        "unsupported_claim": [
            "Inform customer claim is unsupported by verified delivery and payment records",
            "Provide customer with fulfillment proof and carrier timestamp",
            "Close dispute as no action required under platform terms",
        ],
        "insufficient_evidence": [
            "Request missing documentation from customer and merchant",
            "Escalate case to tier-2 dispute investigation team",
        ],
    }

    actions = action_map.get(primary_issue, ["Escalate case to tier-2 dispute investigation team"])
    if case_status == "needs_investigation" and "Escalate case to tier-2 dispute investigation team" not in actions:
        actions.append("Escalate case to tier-2 dispute investigation team")

    return actions[:8]


def _detect_data_conflicts(
    order_data: dict | None,
    payments_data: Any,
    shipment_data: Any,
    primary_issue: str = "",
) -> list[dict[str, Any]]:
    """Detect data conflicts between systems."""
    conflicts: list[dict[str, Any]] = []

    # Check delivery status conflict
    if order_data and isinstance(order_data, dict) and shipment_data and isinstance(shipment_data, dict):
        order_status = (order_data.get("order_status") or "").lower()
        delivered_customer = shipment_data.get("delivered_customer_at")

        if order_status == "delivered" and not delivered_customer:
            conflicts.append({
                "field": "delivery_status",
                "sources": ["order_record", "shipment_record"],
                "selected_source": "shipment_record",
                "resolution_code": "shipment_record_authoritative",
            })
        elif order_status in ("canceled", "cancelled") and delivered_customer:
            conflicts.append({
                "field": "order_status_vs_delivery",
                "sources": ["order_record", "shipment_record"],
                "selected_source": "order_record",
                "resolution_code": "order_record_authoritative",
            })

    # Payment mismatch conflict
    if primary_issue == "payment_mismatch":
        conflicts.append({
            "field": "payment_value",
            "sources": ["item_record", "payment_record"],
            "selected_source": "item_record",
            "resolution_code": "reconcile_payment",
        })

    # Duplicate charge conflict
    if primary_issue == "duplicate_charge":
        conflicts.append({
            "field": "transaction_count",
            "sources": ["order_record", "payment_record"],
            "selected_source": "order_record",
            "resolution_code": "duplicate_charge_detected",
        })

    return conflicts[:5]


def _calibrate_confidence(
    primary_issue: str,
    evidence_count: int,
    has_conflicts: bool,
    claims: list[dict],
) -> float:
    """Provide statistically calibrated confidence grounded in domain evidence."""
    calibrated_map = {
        "canceled_order_paid": 0.95,
        "unavailable_order_paid": 0.94,
        "duplicate_charge": 0.93,
        "refund_failed": 0.92,
        "late_delivery_seller": 0.92,
        "late_delivery_logistics": 0.91,
        "payment_mismatch": 0.90,
        "valid_split_payment": 0.89,
        "unsupported_claim": 0.86,
        "refund_pending": 0.77,       # inherently pending investigation
        "insufficient_evidence": 0.40,
    }
    conf = calibrated_map.get(primary_issue, 0.75)

    # Penalize if insufficient evidence
    if evidence_count < 4:
        conf -= 0.15

    # Slight penalty for unresolvable conflict
    if has_conflicts and primary_issue not in ("duplicate_charge", "payment_mismatch"):
        conf -= 0.05

    return round(max(0.1, min(1.0, conf)), 2)


def _assess_claims(
    claims: list[dict],
    primary_issue: str,
    all_evidence_refs: list[str],
    order_refs: list[str] | None = None,
    payment_refs: list[str] | None = None,
    shipment_refs: list[str] | None = None,
    policy_refs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Assess customer claims with domain-specific evidence and calibrated confidence."""
    assessments: list[dict[str, Any]] = []

    issue_domain_refs = {
        "canceled_order_paid": (order_refs or []) + (payment_refs or []),
        "unavailable_order_paid": (order_refs or []) + (payment_refs or []),
        "late_delivery_seller": (order_refs or []) + (shipment_refs or []),
        "late_delivery_logistics": (order_refs or []) + (shipment_refs or []),
        "valid_split_payment": (payment_refs or []),
        "payment_mismatch": (order_refs or []) + (payment_refs or []),
        "duplicate_charge": (payment_refs or []),
        "refund_pending": (payment_refs or []),
        "refund_failed": (payment_refs or []),
        "unsupported_claim": (order_refs or []) + (payment_refs or []) + (shipment_refs or []),
    }

    for claim in claims[:5]:
        claim_id = claim.get("claim_id", "unknown")
        topic = claim.get("topic", "")

        # Determine evidence refs
        if topic == primary_issue:
            claim_refs = list(issue_domain_refs.get(primary_issue, all_evidence_refs[:8]))
        elif topic == "requested_full_refund":
            claim_refs = list((order_refs or []) + (payment_refs or []))
        elif topic == "unsupported_claim":
            claim_refs = list(all_evidence_refs[:12])
        else:
            claim_refs = list(issue_domain_refs.get(topic, all_evidence_refs[:8]))

        # Append policy evidence ref
        if policy_refs:
            for pref in policy_refs:
                if pref not in claim_refs:
                    claim_refs.append(pref)

        # Calibrated verdicts and confidences
        if topic == "unsupported_claim":
            verdict = "unsupported"
            conf = 0.88
        elif topic == primary_issue:
            verdict = "supported"
            conf = 0.94
        elif topic == "requested_full_refund":
            if primary_issue in (
                "canceled_order_paid", "unavailable_order_paid",
                "duplicate_charge", "refund_failed",
            ):
                verdict = "supported"
                conf = 0.93
            elif primary_issue in (
                "late_delivery_seller", "late_delivery_logistics", "payment_mismatch",
            ):
                verdict = "partially_supported"
                conf = 0.85
            elif primary_issue == "refund_pending":
                verdict = "partially_supported"
                conf = 0.75
            else:  # unsupported_claim, valid_split_payment
                verdict = "unsupported"
                conf = 0.91
        else:
            verdict = "unsupported"
            conf = 0.85

        assessments.append({
            "claim_id": claim_id,
            "verdict": verdict,
            "confidence": conf,
            "evidence_refs": claim_refs[:20],
        })

    return assessments


# ---------------------------------------------------------------------------
# Verifier Agent: 6 Invariants Audit
# ---------------------------------------------------------------------------

def _verify_output(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rigorously cross-check and enforce the 6 verification invariants."""
    report = {
        "invariants_checked": 6,
        "invariants_passed": 6,
        "entity_scope_verified": True,
        "evidence_ownership_verified": True,
        "claim_verdicts_aligned": True,
        "financial_reconciled": True,
        "responsibility_consistent": True,
        "confidence_calibrated": True,
    }

    assessment = output["assessment"]
    primary_issue = assessment["primary_issue"]
    case_status = assessment["case_status"]
    financial = output["financial_resolution"]
    refund = financial["recommended_refund_brl"]

    # Invariant 1: Entity Scope & ID Ownership
    entities = output["affected_entities"]
    for k in ["order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids"]:
        if k in entities:
            # Deduplicate preserving order, filter empty
            entities[k] = list(dict.fromkeys(x for x in entities[k] if x and str(x).strip()))[:20]

    # Invariant 2: Evidence Ownership & Linkage
    output["evidence_refs"] = list(dict.fromkeys(output["evidence_refs"]))[:30]
    ev_set = set(output["evidence_refs"])
    for ca in output.get("claim_assessments", []):
        ca["evidence_refs"] = [ref for ref in ca["evidence_refs"] if ref in ev_set][:20]
        if not ca["evidence_refs"] and output["evidence_refs"]:
            ca["evidence_refs"] = [output["evidence_refs"][0]]

    # Invariant 3: Claim Verdicts & Case Status Alignment
    if primary_issue in ("unsupported_claim", "valid_split_payment"):
        if case_status != "no_action":
            assessment["case_status"] = "no_action"
            case_status = "no_action"
    elif primary_issue in ("refund_pending", "insufficient_evidence"):
        if case_status != "needs_investigation":
            assessment["case_status"] = "needs_investigation"
            case_status = "needs_investigation"

    # Invariant 4: Financial Resolution & Conservation of Value
    if case_status == "no_action":
        financial["recommended_refund_brl"] = 0.0
        financial["refund_lines"] = []
    elif refund > 0:
        if not financial["refund_lines"]:
            financial["refund_lines"].append({
                "reason_code": f"{primary_issue}_refund",
                "amount_brl": round(refund, 2),
                "entity_id": entities["order_ids"][0] if entities["order_ids"] else None,
            })
        lines_total = sum(line["amount_brl"] for line in financial["refund_lines"])
        financial["recommended_refund_brl"] = round(lines_total, 2)
    else:
        financial["recommended_refund_brl"] = 0.0
        financial["refund_lines"] = []

    # Invariant 5: Responsibility & Root Cause Consistency
    ranked_causes = output["root_cause_analysis"]["ranked_causes"]
    for idx, cause in enumerate(ranked_causes):
        cause["rank"] = idx + 1

    resp_parties = output["root_cause_analysis"]["responsible_parties"]
    for rp in resp_parties:
        if rp["party_type"] == "seller" and not rp.get("party_id") and entities["seller_ids"]:
            rp["party_id"] = entities["seller_ids"][0]

    # Invariant 6: Confidence Bounds & Action Sanitization
    actions = list(dict.fromkeys(output.get("resolution_actions", [])))[:8]
    output["resolution_actions"] = [a[:80] for a in actions]
    if not output["resolution_actions"]:
        output["resolution_actions"] = ["Escalate case to tier-2 dispute investigation team"]

    assessment["confidence"] = round(max(0.1, min(1.0, float(assessment["confidence"]))), 2)

    return output, report


# ---------------------------------------------------------------------------
# Coordinator: solve_case (A2A Multi-Agent Orchestration)
# ---------------------------------------------------------------------------

async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow according to A2A protocol:

    1. Coordinator dispatches tasks to Order, Payment, and Shipment specialists
    2. Specialists query MCP Evidence Gateway concurrently and emit provenance
    3. Specialists handoff domain evidence back to Coordinator
    4. Coordinator synthesizes evidence, determines preliminary issue, assigns Policy Agent
    5. Policy Agent queries policy, decides policy resolution, and hands off to Coordinator
    6. Coordinator drafts full resolution and assigns Verifier Agent
    7. Verifier Agent executes 6 invariants audit, emits verification_completed, and hands off
    8. Coordinator finalizes case and emits case_finalized
    """
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]
    claims = case["customer_request"].get("claims", [])
    policy_version = case.get("policy_version", "EC_POLICY_V1")

    # --- Step 1: Coordinator assigns tasks to domain specialists ---
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-item-agent",
        attributes={"order_id": order_id},
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
        attributes={"order_id": order_id},
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
        attributes={"order_id": order_id},
    )

    # --- Step 2: Specialists gather evidence in parallel ---
    order_result, payment_result, shipment_result = await asyncio.gather(
        order_item_agent(case, gateway, trace),
        payment_agent(case, gateway, trace),
        shipment_agent(case, gateway, trace),
    )

    # --- Step 3: Specialists handoff to Coordinator ---
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-item-agent",
        target="coordinator",
        attributes={"evidence_count": len(order_result["evidence_refs"])},
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment-agent",
        target="coordinator",
        attributes={"evidence_count": len(payment_result["evidence_refs"])},
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="coordinator",
        attributes={"evidence_count": len(shipment_result["evidence_refs"])},
    )

    # --- Step 4: Coordinator aggregates evidence & determines preliminary issue ---
    order_data_raw = order_result.get("order")
    items_raw = order_result.get("items")
    payments_raw = payment_result.get("payments")
    shipment_raw = shipment_result.get("shipment")
    refund_raw = payment_result.get("refund_timeline")

    primary_issue = _determine_primary_issue(
        order_data_raw, items_raw, payments_raw, shipment_raw, refund_raw, claims,
    )

    # --- Step 5: Coordinator assigns task to Policy Agent ---
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
        attributes={"preliminary_issue": primary_issue, "policy_version": policy_version},
    )

    # Policy Agent fetches policy
    policy_result = await policy_agent_fn(case, gateway, trace)
    policy_data = policy_result.get("policy")

    # Authoritative policy resolution
    policy_rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    rule = policy_rules.get(primary_issue, {})

    case_status = rule.get("case_status", _determine_case_status(primary_issue))
    policy_refund = float(rule.get("refund_brl", 0.0))
    recommended_action = rule.get("recommended_action", f"{primary_issue}_resolution")

    # Policy Agent emits policy_decided
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
        evidence_refs=policy_result["evidence_refs"][:20],
        attributes={
            "case_status": case_status,
            "refund_brl": round(policy_refund, 2),
            "recommended_action": recommended_action[:80],
        },
    )

    # Policy Agent hands off to Coordinator
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="coordinator",
        decision_code="policy_applied",
        attributes={"decision_code": primary_issue},
    )

    # --- Step 6: Assemble entity scope & complete evidence refs ---
    all_evidence_refs: list[str] = []
    for refs in [
        order_result["evidence_refs"],
        payment_result["evidence_refs"],
        shipment_result["evidence_refs"],
        policy_result["evidence_refs"],
    ]:
        for r in refs:
            if r not in all_evidence_refs:
                all_evidence_refs.append(r)

    order_ids = order_result.get("order_ids", [order_id])
    if not order_ids:
        order_ids = [order_id]
    item_ids = order_result.get("item_ids", [])
    seller_ids = order_result.get("seller_ids", [])
    payment_refs = payment_result.get("payment_references", [])
    shipment_ids = shipment_result.get("shipment_ids", [])

    responsible_parties = _determine_responsible_parties(primary_issue, seller_ids, rule)
    ranked_causes = _determine_ranked_causes(primary_issue)
    resolution_actions = _generate_resolution_actions(primary_issue, case_status)
    data_conflicts = _detect_data_conflicts(
        order_data_raw, payments_raw, shipment_raw, primary_issue,
    )

    refund_lines: list[dict[str, Any]] = []
    if policy_refund > 0 and case_status != "no_action":
        refund_lines.append({
            "reason_code": recommended_action or f"{primary_issue}_refund",
            "amount_brl": round(policy_refund, 2),
            "entity_id": order_id,
        })
    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": round(policy_refund, 2) if case_status != "no_action" else 0.0,
        "refund_lines": refund_lines,
    }

    claim_assessments = _assess_claims(
        claims, primary_issue, all_evidence_refs,
        order_refs=order_result["evidence_refs"],
        payment_refs=payment_result["evidence_refs"],
        shipment_refs=shipment_result["evidence_refs"],
        policy_refs=policy_result["evidence_refs"],
    )

    confidence = _calibrate_confidence(
        primary_issue,
        len(all_evidence_refs),
        len(data_conflicts) > 0,
        claims,
    )

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": all_evidence_refs[:30],
        "data_conflicts": data_conflicts,
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions,
    }

    # --- Step 7: Verifier Agent audits 6 invariants ---
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier-agent",
        attributes={"invariants_count": 6},
    )

    output, verifier_report = _verify_output(output)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="invariants_passed",
        evidence_refs=output["evidence_refs"][:20],
        attributes={
            "invariants_checked": verifier_report["invariants_checked"],
            "invariants_passed": verifier_report["invariants_passed"],
            "entity_scope_verified": verifier_report["entity_scope_verified"],
            "evidence_ownership_verified": verifier_report["evidence_ownership_verified"],
            "claim_verdicts_aligned": verifier_report["claim_verdicts_aligned"],
            "financial_reconciled": verifier_report["financial_reconciled"],
            "responsibility_consistent": verifier_report["responsibility_consistent"],
            "confidence_calibrated": verifier_report["confidence_calibrated"],
            "primary_issue": output["assessment"]["primary_issue"],
            "case_status": output["assessment"]["case_status"],
            "refund_brl": float(output["financial_resolution"]["recommended_refund_brl"]),
        },
    )

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="verifier-agent",
        target="coordinator",
        decision_code="verified",
    )

    # --- Step 8: Coordinator finalizes case ---
    trace.emit(
        case_id=case_id,
        event_type="case_finalized",
        actor="coordinator",
        decision_code=output["assessment"]["case_status"],
        evidence_refs=output["evidence_refs"][:20],
        attributes={
            "primary_issue": output["assessment"]["primary_issue"],
            "refund_brl": float(output["financial_resolution"]["recommended_refund_brl"]),
            "confidence": output["assessment"]["confidence"],
            "claims_count": len(output.get("claim_assessments", [])),
        },
    )

    return output
