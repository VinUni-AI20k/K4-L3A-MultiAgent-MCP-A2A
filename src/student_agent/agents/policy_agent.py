"""Policy Agent — áp dụng chính sách TMĐT vào từng claim dựa trên evidence thật từ MCP.

Người sở hữu: Nguyên
Interface: Coordinator gọi PolicyAgent.evaluate() với findings từ các agent khác,
           nhận lại PolicyResult chứa verdict, refund, actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter

# --------------------------------------------------------------------------- #
# Các giá trị hợp lệ theo contract                                           #
# --------------------------------------------------------------------------- #

VALID_PRIMARY_ISSUES = frozenset([
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
])

VALID_VERDICTS = frozenset([
    "supported",
    "unsupported",
    "partially_supported",
    "insufficient_evidence",
])

VALID_CASE_STATUSES = frozenset([
    "action_required",
    "no_action",
    "needs_investigation",
])

EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


# --------------------------------------------------------------------------- #
# Data classes                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class ClaimAssessment:
    """Kết quả đánh giá một claim."""

    claim_id: str
    verdict: str       # supported | unsupported | partially_supported | insufficient_evidence
    confidence: float  # 0.0 → 1.0
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass
class PolicyResult:
    """Kết quả tổng hợp từ Policy Agent, trả về Coordinator."""

    claim_assessments: list[ClaimAssessment]
    primary_issue: str       # 1 trong 11 giá trị VALID_PRIMARY_ISSUES
    case_status: str         # action_required | no_action | needs_investigation
    confidence: float        # 0.0 → 1.0
    resolution_actions: list[str]
    financial_resolution: dict[str, Any]
    evidence_refs: list[str]  # tất cả evidence_ref đã dùng

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_assessments": [ca.to_dict() for ca in self.claim_assessments],
            "primary_issue": self.primary_issue,
            "case_status": self.case_status,
            "confidence": self.confidence,
            "resolution_actions": list(self.resolution_actions),
            "financial_resolution": dict(self.financial_resolution),
            "evidence_refs": list(self.evidence_refs),
        }


# --------------------------------------------------------------------------- #
# Policy Agent                                                                 #
# --------------------------------------------------------------------------- #

class PolicyAgent:
    """Áp dụng chính sách TMĐT vào từng claim, dựa trên evidence thật từ MCP.

    Usage (bởi Coordinator):
        agent = PolicyAgent(gateway, trace)
        result = await agent.evaluate(
            case_id="L3A_CASE_001",
            policy_version="EC_POLICY_V1",
            claims=[{"claim_id": "claim-001-a", "topic": "canceled_order_paid"}],
            findings={"order": {...}, "payment": {...}, ...}
        )
    """

    ACTOR_NAME = "policy-agent"

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self._gateway = gateway
        self._trace = trace

    # ---- Entry point chính ------------------------------------------------ #

    async def evaluate(
        self,
        *,
        case_id: str,
        policy_version: str,
        claims: list[dict[str, Any]],
        findings: dict[str, dict[str, Any]],
    ) -> PolicyResult:
        """Đánh giá tất cả claims dựa trên policy và evidence.

        Args:
            case_id: ID của case đang xử lý.
            policy_version: Phiên bản policy (vd: "EC_POLICY_V1").
            claims: Danh sách claims từ customer_request.
            findings: Dict chứa evidence từ các agent khác.
                      Key: "order", "payment", "shipment", "items", "seller"
                      Value: {"data": {...}, "evidence_ref": "ev_..."}

        Returns:
            PolicyResult chứa verdict, refund, actions.
        """
        all_evidence_refs: list[str] = []

        # ---- Bước 1: Lấy policy từ MCP ----------------------------------- #
        policy_data, policy_ref = await self._fetch_policy(case_id, policy_version)
        all_evidence_refs.append(policy_ref)

        # Thu thập evidence_refs từ findings
        for domain_key, finding in findings.items():
            ref = finding.get("evidence_ref")
            if ref and isinstance(ref, str):
                all_evidence_refs.append(ref)

        # ---- Bước 2: Đánh giá từng claim --------------------------------- #
        claim_assessments: list[ClaimAssessment] = []
        for claim in claims:
            assessment = self._evaluate_single_claim(
                claim=claim,
                findings=findings,
                policy_data=policy_data,
                policy_ref=policy_ref,
                all_evidence_refs=all_evidence_refs,
            )
            claim_assessments.append(assessment)

        # ---- Bước 3: Xác định primary_issue ------------------------------ #
        primary_issue = self._determine_primary_issue(claims, claim_assessments, findings)

        # ---- Bước 4: Xác định case_status -------------------------------- #
        case_status = self._determine_case_status(claim_assessments)

        # ---- Bước 5: Tính overall confidence ------------------------------ #
        overall_confidence = self._compute_overall_confidence(claim_assessments)

        # ---- Bước 6: Tính financial_resolution ---------------------------- #
        financial_resolution = self._compute_financial_resolution(
            claim_assessments=claim_assessments,
            findings=findings,
            primary_issue=primary_issue,
            case_status=case_status,
        )

        # ---- Bước 7: Đề xuất resolution_actions -------------------------- #
        resolution_actions = self._determine_resolution_actions(
            primary_issue=primary_issue,
            case_status=case_status,
            claim_assessments=claim_assessments,
            financial_resolution=financial_resolution,
        )

        # ---- Bước 8: Emit trace "policy_decided" ------------------------- #
        self._trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=self.ACTOR_NAME,
            decision_code=primary_issue,
            evidence_refs=_unique_refs(all_evidence_refs),
        )

        return PolicyResult(
            claim_assessments=claim_assessments,
            primary_issue=primary_issue,
            case_status=case_status,
            confidence=overall_confidence,
            resolution_actions=resolution_actions,
            financial_resolution=financial_resolution,
            evidence_refs=_unique_refs(all_evidence_refs),
        )

    # ---- Lấy policy từ MCP ----------------------------------------------- #

    async def _fetch_policy(
        self, case_id: str, policy_version: str
    ) -> tuple[dict[str, Any], str]:
        """Gọi MCP get_policy và emit trace."""
        evidence = await self._gateway.call(
            "get_policy",
            case_id=case_id,
            policy_version=policy_version,
        )
        evidence_ref: str = evidence["evidence_ref"]
        policy_data: dict[str, Any] = evidence["data"]

        self._trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=self.ACTOR_NAME,
            tool_name="get_policy",
            evidence_refs=[evidence_ref],
        )
        return policy_data, evidence_ref

    # ---- Đánh giá một claim ---------------------------------------------- #

    def _evaluate_single_claim(
        self,
        *,
        claim: dict[str, Any],
        findings: dict[str, dict[str, Any]],
        policy_data: dict[str, Any],
        policy_ref: str,
        all_evidence_refs: list[str],
    ) -> ClaimAssessment:
        """Đánh giá một claim dựa trên evidence và policy."""
        claim_id: str = claim["claim_id"]
        topic: str = claim.get("topic", "")

        # Thu thập evidence_refs liên quan cho claim này
        claim_refs: list[str] = [policy_ref]

        # Lấy dữ liệu từ findings
        order_data = _safe_get_data(findings, "order")
        payment_data = _safe_get_data(findings, "payment")
        shipment_data = _safe_get_data(findings, "shipment")

        order_ref = _safe_get_ref(findings, "order")
        payment_ref = _safe_get_ref(findings, "payment")
        shipment_ref = _safe_get_ref(findings, "shipment")

        # Dispatch theo topic
        if topic == "canceled_order_paid":
            verdict, confidence = self._check_canceled_order_paid(order_data, payment_data)
            if order_ref:
                claim_refs.append(order_ref)
            if payment_ref:
                claim_refs.append(payment_ref)

        elif topic == "unavailable_order_paid":
            verdict, confidence = self._check_unavailable_order_paid(order_data, payment_data)
            if order_ref:
                claim_refs.append(order_ref)
            if payment_ref:
                claim_refs.append(payment_ref)

        elif topic == "late_delivery_seller":
            verdict, confidence = self._check_late_delivery_seller(order_data, shipment_data)
            if order_ref:
                claim_refs.append(order_ref)
            if shipment_ref:
                claim_refs.append(shipment_ref)

        elif topic == "late_delivery_logistics":
            verdict, confidence = self._check_late_delivery_logistics(order_data, shipment_data)
            if order_ref:
                claim_refs.append(order_ref)
            if shipment_ref:
                claim_refs.append(shipment_ref)

        elif topic == "valid_split_payment":
            verdict, confidence = self._check_valid_split_payment(order_data, payment_data)
            if order_ref:
                claim_refs.append(order_ref)
            if payment_ref:
                claim_refs.append(payment_ref)

        elif topic == "payment_mismatch":
            verdict, confidence = self._check_payment_mismatch(order_data, payment_data)
            if order_ref:
                claim_refs.append(order_ref)
            if payment_ref:
                claim_refs.append(payment_ref)

        elif topic == "duplicate_charge":
            verdict, confidence = self._check_duplicate_charge(payment_data)
            if payment_ref:
                claim_refs.append(payment_ref)

        elif topic in ("refund_pending", "refund_failed"):
            verdict, confidence = self._check_refund_status(topic, payment_data)
            if payment_ref:
                claim_refs.append(payment_ref)

        elif topic == "requested_full_refund":
            verdict, confidence = self._check_requested_full_refund(
                order_data, payment_data, policy_data,
            )
            if order_ref:
                claim_refs.append(order_ref)
            if payment_ref:
                claim_refs.append(payment_ref)

        else:
            # Topic không nằm trong danh sách → unsupported_claim
            verdict = "unsupported"
            confidence = 0.8

        return ClaimAssessment(
            claim_id=claim_id,
            verdict=verdict,
            confidence=_clamp(confidence),
            evidence_refs=_unique_refs(claim_refs),
        )

    # ---- Các hàm kiểm tra theo topic ------------------------------------- #

    def _check_canceled_order_paid(
        self,
        order: dict[str, Any] | None,
        payment: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Đơn bị hủy nhưng đã thanh toán?"""
        if order is None or payment is None:
            return "insufficient_evidence", 0.3

        order_status = _normalize(order.get("order_status", ""))
        payment_value = _to_float(payment.get("payment_value", 0))

        if order_status == "canceled" and payment_value > 0:
            return "supported", 0.95
        if order_status == "canceled" and payment_value == 0:
            return "partially_supported", 0.7
        return "unsupported", 0.85

    def _check_unavailable_order_paid(
        self,
        order: dict[str, Any] | None,
        payment: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Sản phẩm không có sẵn nhưng đã thanh toán?"""
        if order is None or payment is None:
            return "insufficient_evidence", 0.3

        order_status = _normalize(order.get("order_status", ""))
        payment_value = _to_float(payment.get("payment_value", 0))

        if order_status == "unavailable" and payment_value > 0:
            return "supported", 0.92
        if order_status in ("canceled", "unavailable"):
            return "partially_supported", 0.6
        return "unsupported", 0.85

    def _check_late_delivery_seller(
        self,
        order: dict[str, Any] | None,
        shipment: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Giao trễ — lỗi seller (seller gửi hàng muộn)?"""
        if order is None or shipment is None:
            return "insufficient_evidence", 0.3

        estimated = order.get("order_estimated_delivery_date", "")
        delivered = order.get("order_delivered_customer_date", "")
        shipped = shipment.get("shipping_limit_date", "")
        actual_ship = order.get("order_delivered_carrier_date", "")

        if not estimated or not delivered:
            return "insufficient_evidence", 0.4

        if delivered > estimated:
            # Giao trễ — kiểm tra seller có ship trễ không
            if actual_ship and shipped and actual_ship > shipped:
                return "supported", 0.90
            return "partially_supported", 0.6
        return "unsupported", 0.85

    def _check_late_delivery_logistics(
        self,
        order: dict[str, Any] | None,
        shipment: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Giao trễ — lỗi logistics (seller gửi đúng hạn, carrier trễ)?"""
        if order is None or shipment is None:
            return "insufficient_evidence", 0.3

        estimated = order.get("order_estimated_delivery_date", "")
        delivered = order.get("order_delivered_customer_date", "")
        shipped = shipment.get("shipping_limit_date", "")
        actual_ship = order.get("order_delivered_carrier_date", "")

        if not estimated or not delivered:
            return "insufficient_evidence", 0.4

        if delivered > estimated:
            # Giao trễ — kiểm tra seller đã ship đúng hạn chưa
            if actual_ship and shipped and actual_ship <= shipped:
                return "supported", 0.88
            return "partially_supported", 0.55
        return "unsupported", 0.85

    def _check_valid_split_payment(
        self,
        order: dict[str, Any] | None,
        payment: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Thanh toán chia nhỏ hợp lệ?"""
        if order is None or payment is None:
            return "insufficient_evidence", 0.3

        # payment có thể là list (nhiều payment) hoặc dict
        payments = payment if isinstance(payment, list) else [payment]
        if len(payments) > 1:
            total = sum(_to_float(p.get("payment_value", 0)) for p in payments)
            order_value = _to_float(order.get("price", 0)) + _to_float(order.get("freight_value", 0))
            if order_value > 0 and abs(total - order_value) < 0.01:
                return "supported", 0.90
            return "partially_supported", 0.6
        return "unsupported", 0.7

    def _check_payment_mismatch(
        self,
        order: dict[str, Any] | None,
        payment: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Số tiền thanh toán không khớp order value?"""
        if order is None or payment is None:
            return "insufficient_evidence", 0.3

        payments = payment if isinstance(payment, list) else [payment]
        total_paid = sum(_to_float(p.get("payment_value", 0)) for p in payments)
        order_value = _to_float(order.get("price", 0)) + _to_float(order.get("freight_value", 0))

        if order_value > 0 and abs(total_paid - order_value) > 0.01:
            return "supported", 0.88
        return "unsupported", 0.85

    def _check_duplicate_charge(
        self,
        payment: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Có ≥2 payment cùng amount cho 1 order?"""
        if payment is None:
            return "insufficient_evidence", 0.3

        payments = payment if isinstance(payment, list) else [payment]
        values = [_to_float(p.get("payment_value", 0)) for p in payments]
        if len(values) >= 2 and len(values) != len(set(values)):
            return "supported", 0.85
        return "unsupported", 0.80

    def _check_refund_status(
        self,
        topic: str,
        payment: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Refund pending hoặc failed?"""
        if payment is None:
            return "insufficient_evidence", 0.3

        payments = payment if isinstance(payment, list) else [payment]
        for p in payments:
            ptype = _normalize(p.get("payment_type", ""))
            if ptype == "refund" or "refund" in ptype:
                # Nếu topic = refund_pending → xem có pending refund không
                # Nếu topic = refund_failed → xem có failed refund không
                return "supported", 0.80
        return "unsupported", 0.75

    def _check_requested_full_refund(
        self,
        order: dict[str, Any] | None,
        payment: dict[str, Any] | None,
        policy: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Khách yêu cầu hoàn tiền — kiểm tra policy có cho phép?"""
        if order is None or payment is None:
            return "insufficient_evidence", 0.3

        order_status = _normalize(order.get("order_status", ""))

        # Nếu order canceled/unavailable → policy thường cho phép refund
        if order_status in ("canceled", "unavailable"):
            return "supported", 0.90

        # Nếu order delivered → không đủ cơ sở refund toàn phần
        if order_status == "delivered":
            return "partially_supported", 0.5

        return "needs_investigation", 0.4

    # ---- Xác định primary_issue ------------------------------------------ #

    def _determine_primary_issue(
        self,
        claims: list[dict[str, Any]],
        assessments: list[ClaimAssessment],
        findings: dict[str, dict[str, Any]],
    ) -> str:
        """Chọn primary_issue dựa trên claim topics và kết quả đánh giá."""
        # Ưu tiên claim "supported" có confidence cao nhất
        supported = [
            (claims[i].get("topic", ""), a)
            for i, a in enumerate(assessments)
            if a.verdict == "supported" and i < len(claims)
        ]

        if supported:
            # Chọn claim supported có confidence cao nhất
            best_topic, best_assessment = max(supported, key=lambda x: x[1].confidence)
            if best_topic in VALID_PRIMARY_ISSUES:
                return best_topic

        # Fallback: lấy topic đầu tiên nếu hợp lệ
        for claim in claims:
            topic = claim.get("topic", "")
            if topic in VALID_PRIMARY_ISSUES:
                return topic

        # Nếu không có topic hợp lệ
        any_insufficient = any(a.verdict == "insufficient_evidence" for a in assessments)
        if any_insufficient:
            return "insufficient_evidence"
        return "unsupported_claim"

    # ---- Xác định case_status -------------------------------------------- #

    def _determine_case_status(
        self,
        assessments: list[ClaimAssessment],
    ) -> str:
        """Xác định case_status dựa trên tổng hợp verdicts."""
        verdicts = [a.verdict for a in assessments]

        if any(v == "supported" for v in verdicts):
            return "action_required"
        if any(v == "partially_supported" for v in verdicts):
            return "needs_investigation"
        if any(v == "insufficient_evidence" for v in verdicts):
            return "needs_investigation"
        return "no_action"

    # ---- Tính overall confidence ----------------------------------------- #

    def _compute_overall_confidence(
        self,
        assessments: list[ClaimAssessment],
    ) -> float:
        """Trung bình confidence của tất cả claims."""
        if not assessments:
            return 0.0
        return _clamp(sum(a.confidence for a in assessments) / len(assessments))

    # ---- Tính financial_resolution --------------------------------------- #

    def _compute_financial_resolution(
        self,
        *,
        claim_assessments: list[ClaimAssessment],
        findings: dict[str, dict[str, Any]],
        primary_issue: str,
        case_status: str,
    ) -> dict[str, Any]:
        """Tính refund dựa trên verdict và evidence."""
        refund_lines: list[dict[str, Any]] = []
        total_refund = 0.0

        if case_status == "no_action":
            return {
                "currency": "BRL",
                "recommended_refund_brl": 0.0,
                "refund_lines": [],
            }

        # Lấy payment data để tính refund
        payment_data = _safe_get_data(findings, "payment")
        order_data = _safe_get_data(findings, "order")

        for assessment in claim_assessments:
            if assessment.verdict in ("supported", "partially_supported"):
                amount = self._calculate_refund_for_claim(
                    assessment=assessment,
                    payment_data=payment_data,
                    order_data=order_data,
                    primary_issue=primary_issue,
                )
                if amount > 0:
                    # Xác định entity_id liên quan
                    entity_id = None
                    if order_data:
                        entity_id = order_data.get("order_id")

                    refund_lines.append({
                        "reason_code": primary_issue,
                        "amount_brl": round(amount, 2),
                        "entity_id": entity_id,
                    })
                    total_refund += amount

        return {
            "currency": "BRL",
            "recommended_refund_brl": round(total_refund, 2),
            "refund_lines": refund_lines,
        }

    def _calculate_refund_for_claim(
        self,
        *,
        assessment: ClaimAssessment,
        payment_data: dict[str, Any] | None,
        order_data: dict[str, Any] | None,
        primary_issue: str,
    ) -> float:
        """Tính refund cho một claim cụ thể."""
        if payment_data is None:
            return 0.0

        payments = payment_data if isinstance(payment_data, list) else [payment_data]
        total_paid = sum(_to_float(p.get("payment_value", 0)) for p in payments)

        if assessment.verdict == "supported":
            # Full refund nếu supported
            return total_paid
        if assessment.verdict == "partially_supported":
            # Partial refund (50%) nếu partially_supported
            return round(total_paid * 0.5, 2)
        return 0.0

    # ---- Đề xuất resolution_actions -------------------------------------- #

    def _determine_resolution_actions(
        self,
        *,
        primary_issue: str,
        case_status: str,
        claim_assessments: list[ClaimAssessment],
        financial_resolution: dict[str, Any],
    ) -> list[str]:
        """Tạo danh sách actions dựa trên kết luận."""
        actions: list[str] = []

        if case_status == "no_action":
            actions.append("close_case")
            actions.append("notify_customer_no_action")
            return actions

        refund = financial_resolution.get("recommended_refund_brl", 0)

        if case_status == "action_required":
            if refund > 0:
                actions.append("issue_refund")
            actions.append("notify_customer")

            if primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
                actions.append("cancel_order_confirm")
            elif primary_issue in ("late_delivery_seller",):
                actions.append("warn_seller")
                actions.append("extend_delivery_guarantee")
            elif primary_issue in ("late_delivery_logistics",):
                actions.append("escalate_logistics")
                actions.append("extend_delivery_guarantee")
            elif primary_issue in ("payment_mismatch", "duplicate_charge"):
                actions.append("escalate_payment_review")
            elif primary_issue in ("refund_pending", "refund_failed"):
                actions.append("retry_refund")

        elif case_status == "needs_investigation":
            actions.append("escalate_for_review")
            actions.append("notify_customer_investigation")

        return actions[:8]  # max 8 theo schema


# --------------------------------------------------------------------------- #
# Helper functions                                                             #
# --------------------------------------------------------------------------- #

def _safe_get_data(
    findings: dict[str, dict[str, Any]], key: str
) -> dict[str, Any] | None:
    """Lấy data từ findings[key]["data"], trả None nếu không có."""
    entry = findings.get(key)
    if entry is None:
        return None
    return entry.get("data")


def _safe_get_ref(findings: dict[str, dict[str, Any]], key: str) -> str | None:
    """Lấy evidence_ref từ findings[key]["evidence_ref"]."""
    entry = findings.get(key)
    if entry is None:
        return None
    ref = entry.get("evidence_ref")
    if ref and isinstance(ref, str) and EVIDENCE_REF_PATTERN.match(ref):
        return ref
    return None


def _normalize(value: str) -> str:
    """Lowercase + strip cho so sánh."""
    return value.strip().lower()


def _to_float(value: Any) -> float:
    """Chuyển đổi an toàn sang float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Giới hạn value trong [min_val, max_val]."""
    return max(min_val, min(max_val, value))


def _unique_refs(refs: list[str]) -> list[str]:
    """Loại bỏ duplicate, giữ thứ tự."""
    seen: set[str] = set()
    result: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            result.append(ref)
    return result
