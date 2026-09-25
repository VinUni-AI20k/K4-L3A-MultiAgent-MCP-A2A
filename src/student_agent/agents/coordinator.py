from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import OrderLogisticsResult, PaymentResolutionResult
from ..trace import TraceWriter
from .order_logistics import OrderLogisticsAgent
from .payment_resolution import PaymentResolutionAgent
from .verifier import Verifier

logger = logging.getLogger(__name__)


class CoordinatorAgent:
    """Agent Điều phối trung tâm A2A và tổng hợp kết luận cuối cùng (Chử Trần Phương Nam)."""

    def __init__(self, verifier: Verifier, actor_name: str = "coordinator") -> None:
        self.actor_name = actor_name
        self.verifier = verifier
        self.order_agent = OrderLogisticsAgent()
        self.payment_agent = PaymentResolutionAgent()

    async def coordinate(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        case_id = case["case_id"]

        # Pha 1: Phân công Order & Logistics Agent (Ngụy Khắc Phi Long)
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor=self.actor_name,
            target=self.order_agent.actor_name,
        )
        order_res: OrderLogisticsResult = await self.order_agent.investigate(case, gateway, trace)
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.order_agent.actor_name,
            target=self.actor_name,
        )

        # Pha 2: Phân công Payment & Resolution Agent (Nguyễn Đức Phát) kèm theo context đơn hàng
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor=self.actor_name,
            target=self.payment_agent.actor_name,
        )
        payment_res: PaymentResolutionResult = await self.payment_agent.investigate(
            case, gateway, trace, order_res
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.payment_agent.actor_name,
            target=self.actor_name,
        )

        # Pha 3: Tổng hợp phán quyết (Synthesis & Root Cause)
        primary_issue, case_status, confidence = self._determine_assessment(case, order_res, payment_res)
        root_cause = self._build_root_cause(primary_issue, order_res, payment_res)

        # Gom toàn bộ bằng chứng
        all_evidence = list(dict.fromkeys(order_res.evidence_refs + payment_res.evidence_refs))

        # Gom thực thể
        affected_entities = {
            "order_ids": order_res.order_ids,
            "item_ids": order_res.item_ids,
            "seller_ids": order_res.seller_ids,
            "payment_references": payment_res.payment_references,
            "shipment_ids": order_res.shipment_ids,
        }

        # Resolution actions
        actions = list(payment_res.resolution_actions)
        if primary_issue == "late_delivery_seller":
            actions = ["penalize_seller", "notify_customer"]
        elif primary_issue == "late_delivery_logistics":
            actions = ["file_carrier_claim", "notify_customer"]
        elif primary_issue == "valid_split_payment" or case_status == "no_action":
            actions = ["notify_customer"]
        elif not actions:
            actions = ["investigate_further"]

        raw_output: dict[str, Any] = {
            "schema_version": "day09-l3a-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": primary_issue,
                "case_status": case_status,
                "confidence": confidence,
            },
            "affected_entities": affected_entities,
            "claim_assessments": payment_res.claim_assessments,
            "root_cause_analysis": root_cause,
            "evidence_refs": all_evidence,
            "data_conflicts": [],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": payment_res.recommended_refund_brl if case_status != "no_action" else 0.0,
                "refund_lines": payment_res.refund_lines if case_status != "no_action" else [],
            },
            "resolution_actions": actions,
        }

        # Pha 4: Thẩm định an toàn (Đỗ Thành Đạt)
        verified_output = self.verifier.verify(raw_output)
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
        )

        return verified_output

    def _determine_assessment(
        self, case: dict[str, Any], order_res: OrderLogisticsResult, pay_res: PaymentResolutionResult
    ) -> tuple[str, str, float]:
        """Quyết định primary_issue dựa trên việc xác thực Claim mà khách khiếu nại."""
        customer_req = case.get("customer_request", {})
        claims = customer_req.get("claims", [])
        
        # Lấy claimed topic cốt lõi (bỏ qua requested_full_refund vì đó là yêu cầu bồi thường)
        claimed_topic = None
        for cl in claims:
            t = cl.get("topic")
            if t and t != "requested_full_refund":
                claimed_topic = t
                break

        # 1. Nếu khách khiếu nại về giao hàng trễ
        if claimed_topic in ["late_delivery_seller", "late_delivery_logistics"]:
            if order_res.is_late:
                target_issue = order_res.suggested_issue or claimed_topic
                return target_issue, "action_required", 0.95
            else:
                return "unsupported_claim", "no_action", 0.90

        # 2. Nếu khách khiếu nại về đơn bị hủy/không có hàng
        if claimed_topic in ["canceled_order_paid", "unavailable_order_paid"]:
            if order_res.order_status in ["canceled", "unavailable"]:
                return claimed_topic, "action_required", 0.95
            else:
                return "unsupported_claim", "no_action", 0.90

        # 3. Nếu khách khiếu nại về hoàn tiền (pending hoặc failed)
        if claimed_topic == "refund_pending":
            if pay_res.refund_status == "pending":
                return "refund_pending", "needs_investigation", 0.95
            return "unsupported_claim", "no_action", 0.90

        if claimed_topic == "refund_failed":
            if pay_res.refund_status == "failed":
                return "refund_failed", "action_required", 0.95
            return "unsupported_claim", "no_action", 0.90

        # 4. Nếu khách khiếu nại về trừ trùng (duplicate charge)
        if claimed_topic == "duplicate_charge":
            if pay_res.is_duplicate_charge:
                return "duplicate_charge", "action_required", 0.95
            return "unsupported_claim", "no_action", 0.90

        # 5. Nếu khách khiếu nại về chia thanh toán hợp lệ (valid_split_payment)
        if claimed_topic == "valid_split_payment":
            return "valid_split_payment", "no_action", 0.95

        # 6. Nếu khách khiếu nại về lệch tiền thanh toán (payment_mismatch)
        if claimed_topic == "payment_mismatch":
            if pay_res.is_payment_mismatch:
                return "payment_mismatch", "action_required", 0.95
            return "unsupported_claim", "no_action", 0.90

        # 7. Nếu khách khiếu nại vô căn cứ (unsupported_claim)
        if claimed_topic == "unsupported_claim":
            return "unsupported_claim", "no_action", 0.95

        # Fallback dựa trên phát hiện của agent nếu không có claim rõ ràng
        if pay_res.suggested_issue:
            st = "needs_investigation" if pay_res.suggested_issue == "refund_pending" else "action_required"
            return pay_res.suggested_issue, st, 0.85

        if order_res.suggested_issue:
            return order_res.suggested_issue, "action_required", 0.85

        return "unsupported_claim", "no_action", 0.80

    def _build_root_cause(
        self, primary_issue: str, order_res: OrderLogisticsResult, pay_res: PaymentResolutionResult
    ) -> dict[str, Any]:
        """Xây dựng phân tích nguyên nhân gốc rễ và bên chịu trách nhiệm."""
        responsible_parties: list[dict[str, Any]] = []

        if primary_issue == "late_delivery_seller":
            seller_id = order_res.seller_ids[0] if order_res.seller_ids else None
            responsible_parties.append({"party_type": "seller", "party_id": seller_id})
            cause_code = "SELLER_HANDOFF_DELAY"
        elif primary_issue == "late_delivery_logistics":
            responsible_parties.append({"party_type": "logistics_provider", "party_id": None})
            cause_code = "CARRIER_TRANSIT_DELAY"
        elif primary_issue in ["canceled_order_paid", "unavailable_order_paid"]:
            responsible_parties.append({"party_type": "platform", "party_id": None})
            cause_code = "AUTO_REFUND_FAILURE"
        elif primary_issue == "duplicate_charge":
            responsible_parties.append({"party_type": "payment_provider", "party_id": None})
            cause_code = "PAYMENT_GATEWAY_DUPLICATE_AUTH"
        elif primary_issue == "payment_mismatch":
            responsible_parties.append({"party_type": "platform", "party_id": None})
            cause_code = "CHECKOUT_TOTAL_CALCULATION_MISMATCH"
        elif primary_issue == "refund_pending":
            responsible_parties.append({"party_type": "payment_provider", "party_id": None})
            cause_code = "BANK_PROCESSING_DELAY"
        elif primary_issue == "refund_failed":
            responsible_parties.append({"party_type": "payment_provider", "party_id": None})
            cause_code = "CARD_ISSUER_REJECTED_REFUND"
        elif primary_issue == "valid_split_payment":
            responsible_parties.append({"party_type": "customer", "party_id": None})
            cause_code = "CUSTOMER_AUTHORIZED_MULTI_PAYMENT"
        else:
            responsible_parties.append({"party_type": "customer", "party_id": None})
            cause_code = "CUSTOMER_MISUNDERSTANDING"

        return {
            "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
            "responsible_parties": responsible_parties,
        }
