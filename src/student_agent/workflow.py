"""L3A Multi-Agent Workflow – Coordinator & Specialist Agents.

Kiến trúc:
  Coordinator
      │
      ├─► OrderAgent          (order/item/seller data)
      ├─► PaymentAgent        (payment & financial resolution)  ← THÀNH VIÊN 4
      ├─► ShipmentAgent       (logistics & delivery)
      ├─► PolicyAgent         (policy decisions & actions)
      └─► Verifier            (cross-field consistency check)

Luồng solve_case():
  1. coordinator  nhận case, phân tích yêu cầu khách hàng.
  2. order_agent  lấy order/items/sellers → xác định order_id, invoice amount, status.
  3. payment_agent lấy payments/timelines/refunds → financial_resolution.
  4. shipment_agent lấy logistics → late-delivery classification.
  5. policy_agent  quyết định action, responsible party.
  6. verifier      kiểm tra invariants trước khi finalize.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from . import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from .agents.payment_agent import PaymentAgent
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_PAYMENT_AGENT = PaymentAgent()

_PRIMARY_ISSUE_ALLOWED = {
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

_CASE_STATUS_ALLOWED = {"action_required", "no_action", "needs_investigation"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pick_primary_issue(
    *,
    order_status: Optional[str],
    payment_issue: Optional[str],
    is_late_delivery: bool,
    late_by_logistics: bool,
) -> str:
    """Chọn primary_issue theo thứ tự ưu tiên nghiệp vụ."""
    if payment_issue == "duplicate_charge":
        return "duplicate_charge"
    if payment_issue == "refund_failed":
        return "refund_failed"
    if payment_issue == "refund_pending":
        return "refund_pending"
    if order_status == "canceled":
        return "canceled_order_paid"
    if order_status == "unavailable":
        return "unavailable_order_paid"
    if payment_issue == "payment_mismatch":
        return "payment_mismatch"
    if payment_issue == "valid_split_payment":
        return "valid_split_payment"
    if is_late_delivery:
        return "late_delivery_logistics" if late_by_logistics else "late_delivery_seller"
    return "insufficient_evidence"


def _case_status_from_issue(issue: str, refund_brl: float) -> str:
    """Suy ra case_status từ issue và số tiền hoàn."""
    if issue in {
        "canceled_order_paid", "unavailable_order_paid",
        "duplicate_charge", "refund_failed", "refund_pending",
        "payment_mismatch", "late_delivery_seller", "late_delivery_logistics",
    }:
        if refund_brl > 0 or issue in {"refund_failed", "refund_pending"}:
            return "action_required"
    if issue in {"valid_split_payment"}:
        return "no_action"
    if issue == "insufficient_evidence":
        return "needs_investigation"
    return "no_action"


def _extract_order_id(case: dict[str, Any]) -> Optional[str]:
    """Trích order_id từ case input (field có thể khác nhau tuỳ format)."""
    return (
        case.get("order_id")
        or case.get("order_ids", [None])[0]
        or None
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stub: Order Agent (Member 1/2 sẽ replace bằng OrderAgent thật)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_order_agent(
    *,
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Gọi order/item/seller tools để lấy thông tin đơn hàng.

    Trả về dict chuẩn để PaymentAgent và các specialist khác dùng.
    Member 1/2 replace stub này bằng OrderAgent thật.
    """
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
    )

    evidence_refs: list[str] = []
    order_data: dict[str, Any] = {}
    items_data: list[dict[str, Any]] = []

    try:
        order_ev = await gateway.call("get_order", case_id=case_id, order_id=order_id)
        ref = order_ev.get("evidence_ref", "")
        if ref:
            evidence_refs.append(ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order-agent",
                tool_name="get_order",
                evidence_refs=[ref],
            )
        order_data = order_ev.get("data", {}) or {}
        if isinstance(order_data, list):
            order_data = order_data[0] if order_data else {}
    except Exception as exc:
        logger.warning("order-agent: get_order failed: %s", exc)

    try:
        items_ev = await gateway.call("get_order_items", case_id=case_id, order_id=order_id)
        ref = items_ev.get("evidence_ref", "")
        if ref:
            evidence_refs.append(ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order-agent",
                tool_name="get_order_items",
                evidence_refs=[ref],
            )
        raw_items = items_ev.get("data", [])
        if isinstance(raw_items, list):
            items_data = [x for x in raw_items if isinstance(x, dict)]
        elif isinstance(raw_items, dict):
            items_data = raw_items.get("items", [raw_items])
    except Exception as exc:
        logger.warning("order-agent: get_order_items failed: %s", exc)

    # Tính tổng invoice từ items
    invoice_total: Optional[float] = None
    item_ids: list[str] = []
    seller_ids: list[str] = []
    for item in items_data:
        price = item.get("price", 0) or 0
        freight = item.get("freight_value", 0) or 0
        invoice_total = (invoice_total or 0) + float(price) + float(freight)
        iid = item.get("order_item_id") or item.get("item_id")
        sid = item.get("seller_id")
        if iid:
            item_ids.append(str(iid))
        if sid and str(sid) not in seller_ids:
            seller_ids.append(str(sid))

    status = (
        order_data.get("order_status")
        or order_data.get("status")
        or "unknown"
    )

    return {
        "order_status":   status,
        "invoice_total":  invoice_total,
        "item_ids":       item_ids,
        "seller_ids":     seller_ids,
        "evidence_refs":  evidence_refs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stub: Shipment Agent (Member 3 sẽ replace)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_shipment_agent(
    *,
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Kiểm tra logistics & giao hàng muộn.

    Member 3 replace stub này bằng ShipmentAgent thật.
    """
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
    )

    evidence_refs: list[str] = []
    shipment_ids:  list[str] = []
    is_late = False
    late_by_logistics = False

    try:
        ship_ev = await gateway.call(
            "get_order_shipment", case_id=case_id, order_id=order_id
        )
        ref = ship_ev.get("evidence_ref", "")
        if ref:
            evidence_refs.append(ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="shipment-agent",
                tool_name="get_order_shipment",
                evidence_refs=[ref],
            )
        data = ship_ev.get("data", {}) or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        sid = data.get("shipment_id") or data.get("id")
        if sid:
            shipment_ids.append(str(sid))
        # Đơn giản: kiểm tra delivered vs estimated
        estimated = data.get("estimated_delivery_date") or data.get("order_estimated_delivery_date")
        delivered = data.get("order_delivered_customer_date") or data.get("delivered_at")
        if estimated and delivered and delivered > estimated:
            is_late = True
            carrier = data.get("carrier") or data.get("logistics_provider")
            late_by_logistics = bool(carrier)
    except Exception as exc:
        logger.warning("shipment-agent: get_order_shipment failed: %s", exc)

    return {
        "shipment_ids":    shipment_ids,
        "is_late":         is_late,
        "late_logistics":  late_by_logistics,
        "evidence_refs":   evidence_refs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────────────────────────

def _verify(output: dict[str, Any], case_id: str) -> list[str]:
    """Kiểm tra cross-field invariants. Trả danh sách lỗi (rỗng = OK)."""
    errors: list[str] = []
    fin = output.get("financial_resolution", {})

    # Invariant 1: currency phải là BRL
    if fin.get("currency") != "BRL":
        errors.append("financial_resolution.currency must be 'BRL'")

    # Invariant 2: DoD – sum(refund_lines) == recommended_refund_brl
    lines = fin.get("refund_lines", [])
    recommended = fin.get("recommended_refund_brl", 0)
    line_sum = round(sum(float(l.get("amount_brl", 0)) for l in lines), 2)
    if abs(line_sum - round(float(recommended), 2)) > 1e-6:
        errors.append(
            f"DoD violated: sum(refund_lines)={line_sum} != recommended={recommended}"
        )

    # Invariant 3: no_action không được có refund
    assessment = output.get("assessment", {})
    case_status = assessment.get("case_status")
    if case_status == "no_action" and float(recommended) > 0:
        errors.append("case_status=no_action but recommended_refund_brl > 0")

    # Invariant 4: action_required phải có evidence_refs
    ev_refs = output.get("evidence_refs", [])
    if case_status == "action_required" and not ev_refs:
        errors.append("case_status=action_required but no evidence_refs")

    # Invariant 5: refund_lines mỗi dòng phải có đủ 3 field
    for i, line in enumerate(lines):
        for required_field in ("reason_code", "amount_brl", "entity_id"):
            if required_field not in line:
                errors.append(f"refund_lines[{i}] missing '{required_field}'")

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Main entrypoint
# ─────────────────────────────────────────────────────────────────────────────

async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Coordinator – phối hợp các specialist agents để điều tra khiếu nại.

    Args:
        case:    Input JSON từ inputs/<case_id>.json
        gateway: EvidenceGateway đã kết nối với MCP server.
        trace:   TraceWriter để ghi audit log.

    Returns:
        dict thoả mãn l3a-output-v2.schema.json.
    """
    case_id: str = case["case_id"]
    all_evidence_refs: list[str] = []

    # ── 0. Coordinator nhận case ─────────────────────────────────────────────
    # (event case_received được emit trong cli.py trước khi gọi solve_case)
    logger.info("[%s] Coordinator started", case_id)

    # ── 1. Xác định order_id ─────────────────────────────────────────────────
    order_id: Optional[str] = _extract_order_id(case)

    # ── 2. Order Agent ────────────────────────────────────────────────────────
    order_result: dict[str, Any] = {}
    if order_id:
        try:
            order_result = await _run_order_agent(
                case_id=case_id,
                order_id=order_id,
                gateway=gateway,
                trace=trace,
            )
            all_evidence_refs.extend(order_result.get("evidence_refs", []))
        except Exception as exc:
            logger.error("[%s] order-agent failed: %s", case_id, exc)

    order_status = order_result.get("order_status") or "unknown"
    invoice_total: Optional[float] = order_result.get("invoice_total")
    item_ids:   list[str] = order_result.get("item_ids", [])
    seller_ids: list[str] = order_result.get("seller_ids", [])

    # ── 3. Payment Agent (THÀNH VIÊN 4) ──────────────────────────────────────
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
    )

    payment_result = None
    if order_id:
        try:
            payment_result = await _PAYMENT_AGENT.investigate(
                case_id=case_id,
                order_id=order_id,
                gateway=gateway,
                trace=trace,
                expected_order_total=invoice_total,
                order_status=order_status,
            )
            all_evidence_refs.extend(payment_result.evidence_refs)
        except Exception as exc:
            logger.error("[%s] payment-agent failed: %s", case_id, exc)

    payment_references: list[str] = payment_result.payment_references if payment_result else []
    financial_resolution: dict[str, Any] = (
        payment_result.financial_resolution
        if payment_result
        else {"currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []}
    )
    payment_issue: Optional[str] = payment_result.detected_issue if payment_result else None

    # Handoff sau khi payment-agent hoàn thành
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment-agent",
        target="shipment-agent",
    )

    # ── 4. Shipment Agent ─────────────────────────────────────────────────────
    shipment_result: dict[str, Any] = {}
    if order_id:
        try:
            shipment_result = await _run_shipment_agent(
                case_id=case_id,
                order_id=order_id,
                gateway=gateway,
                trace=trace,
            )
            all_evidence_refs.extend(shipment_result.get("evidence_refs", []))
        except Exception as exc:
            logger.error("[%s] shipment-agent failed: %s", case_id, exc)

    shipment_ids:    list[str] = shipment_result.get("shipment_ids", [])
    is_late:         bool      = shipment_result.get("is_late", False)
    late_by_logistics: bool    = shipment_result.get("late_logistics", False)

    # Handoff sang verifier
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment-agent",
        target="verifier",
    )

    # ── 5. Tổng hợp Assessment ───────────────────────────────────────────────
    primary_issue = _pick_primary_issue(
        order_status=order_status,
        payment_issue=payment_issue,
        is_late_delivery=is_late,
        late_by_logistics=late_by_logistics,
    )

    # Tính confidence dựa trên số lượng evidence
    n_ev = len(set(all_evidence_refs))
    if n_ev >= 3:
        confidence = 0.85
    elif n_ev == 2:
        confidence = 0.70
    elif n_ev == 1:
        confidence = 0.55
    else:
        confidence = 0.30

    refund_brl = float(financial_resolution.get("recommended_refund_brl", 0))
    case_status = _case_status_from_issue(primary_issue, refund_brl)

    # ── 6. Xây dựng resolution_actions ──────────────────────────────────────
    resolution_actions: list[str] = []
    if refund_brl > 0:
        resolution_actions.append(f"Issue refund of BRL {refund_brl:.2f} to customer")
    if payment_issue == "duplicate_charge":
        resolution_actions.append("Void duplicate charge with payment gateway")
    if payment_issue == "refund_failed":
        resolution_actions.append("Retry failed refund via payment provider")
    if payment_issue == "refund_pending":
        resolution_actions.append("Expedite pending refund with payment provider")
    if order_status == "canceled":
        resolution_actions.append("Confirm order cancellation and process full refund")
    if is_late and not late_by_logistics:
        resolution_actions.append("Flag seller for late dispatch")
    if is_late and late_by_logistics:
        resolution_actions.append("File logistics delay claim with carrier")
    # Dedup & cap at 8
    seen_actions: set[str] = set()
    unique_actions: list[str] = []
    for a in resolution_actions:
        if a not in seen_actions:
            seen_actions.add(a)
            unique_actions.append(a)
    resolution_actions = unique_actions[:8]

    # ── 7. Xây dựng root_cause_analysis ─────────────────────────────────────
    cause_code_map = {
        "duplicate_charge":      "PAYMENT_GATEWAY_DUPLICATE_CAPTURE",
        "refund_failed":         "PAYMENT_GATEWAY_REFUND_FAILURE",
        "refund_pending":        "PAYMENT_GATEWAY_REFUND_DELAY",
        "payment_mismatch":      "PAYMENT_AMOUNT_DISCREPANCY",
        "valid_split_payment":   "MULTI_METHOD_PAYMENT_VALID",
        "canceled_order_paid":   "ORDER_CANCELED_POST_PAYMENT",
        "unavailable_order_paid": "ORDER_UNAVAILABLE_POST_PAYMENT",
        "late_delivery_seller":  "SELLER_LATE_DISPATCH",
        "late_delivery_logistics": "LOGISTICS_CARRIER_DELAY",
        "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
    }
    cause_code = cause_code_map.get(primary_issue, "INSUFFICIENT_EVIDENCE")

    party_type_map = {
        "duplicate_charge":      "payment_provider",
        "refund_failed":         "payment_provider",
        "refund_pending":        "payment_provider",
        "payment_mismatch":      "payment_provider",
        "valid_split_payment":   "customer",
        "canceled_order_paid":   "platform",
        "unavailable_order_paid": "platform",
        "late_delivery_seller":  "seller",
        "late_delivery_logistics": "logistics_provider",
        "insufficient_evidence": "unknown",
    }
    responsible_party_type = party_type_map.get(primary_issue, "unknown")
    responsible_party_id = seller_ids[0] if seller_ids and responsible_party_type == "seller" else None

    root_cause_analysis: dict[str, Any] = {
        "ranked_causes": [
            {"cause_code": cause_code, "rank": 1},
        ],
        "responsible_parties": [
            {"party_type": responsible_party_type, "party_id": responsible_party_id},
        ],
    }

    # ── 8. Verifier ──────────────────────────────────────────────────────────
    # Assemble output trước để verifier kiểm tra
    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status":   case_status,
            "confidence":    confidence,
        },
        "affected_entities": {
            "order_ids":          [order_id] if order_id else [],
            "item_ids":           item_ids[:20],
            "seller_ids":         seller_ids[:20],
            "payment_references": payment_references,
            "shipment_ids":       shipment_ids[:20],
        },
        "root_cause_analysis": root_cause_analysis,
        "evidence_refs":       list(dict.fromkeys(all_evidence_refs))[:30],
        "data_conflicts":      [],
        "financial_resolution": financial_resolution,
        "resolution_actions":  resolution_actions,
    }

    verification_errors = _verify(output, case_id)
    if verification_errors:
        logger.warning("[%s] Verification warnings: %s", case_id, verification_errors)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="PASS" if not verification_errors else "WARN",
        attributes={"error_count": len(verification_errors)},
    )

    logger.info(
        "[%s] Finalized: issue=%s status=%s refund=%.2f",
        case_id, primary_issue, case_status, refund_brl,
    )

    return output
