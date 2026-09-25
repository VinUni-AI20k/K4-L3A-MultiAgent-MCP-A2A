from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import OrderLogisticsResult, PaymentResolutionResult
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


class PaymentResolutionAgent:
    """Agent phụ trách điều tra Tài chính, Đối soát và Hoàn tiền (Nguyễn Đức Phát)."""

    def __init__(self, actor_name: str = "payment-resolution-agent") -> None:
        self.actor_name = actor_name

    async def investigate(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
        order_ctx: OrderLogisticsResult,
    ) -> PaymentResolutionResult:
        case_id = case["case_id"]
        customer_req = case.get("customer_request", {})
        order_id = customer_req.get("claimed_order_id") or order_ctx.order_id
        claims = customer_req.get("claims", [])

        result = PaymentResolutionResult()

        if not order_id:
            return result

        # 0. Lấy policy trước để phục vụ phán quyết tài chính
        policy_version = case.get("policy_version", "EC_POLICY_V1")
        try:
            pol_ev = await gateway.call("get_policy", case_id=case_id, policy_version=policy_version)
            pol_ref = pol_ev.get("evidence_ref")
            if pol_ref:
                result.policy_ev_ref = pol_ref
                result.evidence_refs.append(pol_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor_name,
                    tool_name="get_policy",
                    evidence_refs=[pol_ref],
                )
            pol_data = pol_ev.get("data", {})
            result.policy_rules = pol_data if isinstance(pol_data, dict) else {}
        except Exception as exc:
            logger.info(f"[{case_id}] get_policy error: {exc}")

        # 1. Gọi MCP get_order_payments
        payments: list[dict[str, Any]] = []
        try:
            pay_evidence = await gateway.call("get_order_payments", case_id=case_id, order_id=order_id)
            ev_ref = pay_evidence.get("evidence_ref")
            if ev_ref:
                result.evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor_name,
                    tool_name="get_order_payments",
                    evidence_refs=[ev_ref],
                )

            pay_data = pay_evidence.get("data", {})
            if isinstance(pay_data, list):
                payments = pay_data
            elif isinstance(pay_data, dict):
                payments = pay_data.get("payments", [])
            else:
                payments = []

            for p in payments:
                if isinstance(p, dict):
                    if p_seq := p.get("payment_sequential"):
                        result.payment_references.append(f"{order_id}-{p_seq}")
                    val = float(p.get("payment_value", 0.0))
                    result.total_paid_brl += val

            result.total_paid_brl = round(result.total_paid_brl, 2)
            if len(payments) > 1:
                result.is_split_payment = True

        except Exception as exc:
            logger.warning(f"[{case_id}] get_order_payments error: {exc}")

        # 2. Gọi MCP get_payment_timeline (kiểm tra duplicate charge)
        captured_events: list[dict[str, Any]] = []
        timeline_ev_ref = None
        try:
            timeline_ev = await gateway.call("get_payment_timeline", case_id=case_id, order_id=order_id)
            timeline_ev_ref = timeline_ev.get("evidence_ref")
            if timeline_ev_ref:
                result.evidence_refs.append(timeline_ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor_name,
                    tool_name="get_payment_timeline",
                    evidence_refs=[timeline_ev_ref],
                )
            t_data = timeline_ev.get("data", {})
            captured_events = [
                e for e in t_data.get("events", []) if e.get("event_type") == "captured"
            ]
            t_payments = t_data.get("payments", [])
            if len(captured_events) >= 2 or (len(t_payments) > len(payments) and len(t_payments) >= 2):
                result.is_duplicate_charge = True
        except Exception as exc:
            logger.info(f"[{case_id}] get_payment_timeline error or not available: {exc}")

        # 3. Gọi MCP get_refund_timeline (kiểm tra refund pending / refund failed)
        refund_ev_ref = None
        try:
            refund_ev = await gateway.call("get_refund_timeline", case_id=case_id, order_id=order_id)
            refund_ev_ref = refund_ev.get("evidence_ref")
            if refund_ev_ref:
                result.evidence_refs.append(refund_ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor_name,
                    tool_name="get_refund_timeline",
                    evidence_refs=[refund_ev_ref],
                )
            ref_data = refund_ev.get("data", {})
            for r_event in ref_data.get("events", []):
                st = r_event.get("status")
                if st in ["pending", "failed"]:
                    result.refund_status = st
                    break
        except Exception as exc:
            logger.info(f"[{case_id}] get_refund_timeline error or not available: {exc}")

        # 4. Phân tích đối soát nghiệp vụ tài chính
        order_status = order_ctx.order_status
        expected_total = order_ctx.order_total_brl or float(order_ctx.order_data.get("order_total_value", 0.0))

        # Priority: order-state signals (canceled/unavailable) trump payment-signal (duplicate_charge),
        # because a canceled order naturally generates multi-capture events that look like duplicates.
        if result.refund_status == "pending":
            result.suggested_issue = "refund_pending"
            result.recommended_refund_brl = 0.0
            result.resolution_actions.append("expedite_pending_refund")

        elif result.refund_status == "failed":
            result.suggested_issue = "refund_failed"
            result.recommended_refund_brl = result.total_paid_brl
            result.refund_lines.append(
                {
                    "reason_code": "RETRY_FAILED_REFUND",
                    "amount_brl": result.total_paid_brl,
                    "entity_id": order_id,
                }
            )
            result.resolution_actions.append("retry_refund_payment")

        elif order_status == "canceled" and result.total_paid_brl > 0:
            result.suggested_issue = "canceled_order_paid"
            result.recommended_refund_brl = result.total_paid_brl
            result.refund_lines.append(
                {
                    "reason_code": "REFUND_CANCELED_ORDER",
                    "amount_brl": result.total_paid_brl,
                    "entity_id": order_id,
                }
            )
            result.resolution_actions.append("issue_full_refund")

        elif order_status == "unavailable" and result.total_paid_brl > 0:
            result.suggested_issue = "unavailable_order_paid"
            result.recommended_refund_brl = result.total_paid_brl
            result.refund_lines.append(
                {
                    "reason_code": "REFUND_UNAVAILABLE_ORDER",
                    "amount_brl": result.total_paid_brl,
                    "entity_id": order_id,
                }
            )
            result.resolution_actions.append("issue_full_refund")

        elif result.is_duplicate_charge:
            result.suggested_issue = "duplicate_charge"
            dup_amount = round(result.total_paid_brl / 2.0, 2) if result.total_paid_brl > 0 else 0.0
            result.recommended_refund_brl = dup_amount
            result.refund_lines.append(
                {
                    "reason_code": "REFUND_DUPLICATE_CHARGE",
                    "amount_brl": dup_amount,
                    "entity_id": order_id,
                }
            )
            result.resolution_actions.append("refund_duplicate_charge")

        elif expected_total > 0 and abs(result.total_paid_brl - expected_total) >= 0.50:
            result.is_payment_mismatch = True
            result.suggested_issue = "payment_mismatch"
            diff = round(abs(result.total_paid_brl - expected_total), 2)
            if result.total_paid_brl > expected_total:
                result.recommended_refund_brl = diff
                result.refund_lines.append(
                    {
                        "reason_code": "REFUND_OVERCHARGED_AMOUNT",
                        "amount_brl": diff,
                        "entity_id": order_id,
                    }
                )
                result.resolution_actions.append("refund_overcharged_amount")

        elif result.is_split_payment:
            result.suggested_issue = "valid_split_payment"

        # 5. Đánh giá từng Claim của khách hàng và gắn ĐÚNG evidence tương ứng
        for cl in claims:
            claim_id = cl.get("claim_id", "")
            topic = cl.get("topic", "")

            if topic in ["late_delivery_seller", "late_delivery_logistics"]:
                claim_ev_refs = list(order_ctx.evidence_refs)
                if not claim_ev_refs:
                    verdict, conf = "insufficient_evidence", 0.60
                elif order_ctx.suggested_issue == topic:
                    verdict, conf = "supported", 0.95
                elif order_ctx.is_late:
                    # Delivery confirmed late but responsible party differs
                    verdict, conf = "partially_supported", 0.75
                else:
                    verdict, conf = "unsupported", 0.90

            elif topic in ["refund_pending", "refund_failed"]:
                claim_ev_refs = [refund_ev_ref] if refund_ev_ref else list(result.evidence_refs)
                if not refund_ev_ref:
                    verdict, conf = "insufficient_evidence", 0.60
                elif result.suggested_issue == topic:
                    verdict, conf = "supported", 0.95
                else:
                    verdict, conf = "unsupported", 0.90

            elif topic == "duplicate_charge":
                claim_ev_refs = [timeline_ev_ref] if timeline_ev_ref else list(result.evidence_refs)
                if not timeline_ev_ref:
                    verdict, conf = "insufficient_evidence", 0.60
                elif result.is_duplicate_charge:
                    verdict, conf = "supported", 0.95
                else:
                    verdict, conf = "unsupported", 0.90

            elif topic in ["canceled_order_paid", "unavailable_order_paid"]:
                claim_ev_refs = list(order_ctx.evidence_refs) + list(result.evidence_refs)
                expected_status = "canceled" if topic == "canceled_order_paid" else "unavailable"
                if not order_ctx.evidence_refs:
                    verdict, conf = "insufficient_evidence", 0.60
                elif order_status == expected_status:
                    verdict, conf = "supported", 0.95
                else:
                    verdict, conf = "unsupported", 0.90

            elif topic == "requested_full_refund":
                # Coordinator will override this verdict based on final_refund_brl
                claim_ev_refs = list(result.evidence_refs)
                verdict = "supported" if result.recommended_refund_brl > 0 else "unsupported"
                conf = 0.90

            elif topic == "valid_split_payment":
                claim_ev_refs = list(result.evidence_refs)
                verdict, conf = ("supported", 0.95) if result.is_split_payment else ("unsupported", 0.90)

            elif topic == "payment_mismatch":
                claim_ev_refs = list(result.evidence_refs)
                verdict, conf = ("supported", 0.95) if result.is_payment_mismatch else ("unsupported", 0.90)

            else:
                claim_ev_refs = list(result.evidence_refs)
                verdict, conf = "unsupported", 0.90

            result.claim_assessments.append(
                {
                    "claim_id": claim_id,
                    "verdict": verdict,
                    "confidence": conf,
                    "evidence_refs": list(dict.fromkeys(claim_ev_refs)),
                }
            )

        result.payment_references = list(dict.fromkeys(result.payment_references))
        result.evidence_refs = list(dict.fromkeys(result.evidence_refs))
        return result
