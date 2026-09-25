"""Payment specialist agent.

Owner: Thành viên 4 (feat/tv4-payment-specialist)

Responsibilities:
- Fetch payment details via MCP
- Detect payment issues: mismatch, duplicate charge, split payment, refund status
- Build financial_resolution (recommended_refund_brl + refund_lines)
- Populate payment_references for affected_entities
- Collect evidence_refs from MCP calls

MCP Tools to discover (run ``day09 mcp-tools``):
- get_payments / get_payment (or similar)
- get_refunds / get_refund (or similar)
"""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseAgent


class PaymentAgent(BaseAgent):
    """Specialist agent for payment and refund data gathering."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        super().__init__(name="payment-agent", gateway=gateway, trace=trace)

    async def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Fetch and analyse payment + refund data.

        Args:
            case_id: The case identifier.
            context: Must contain ``order_id``.

        Returns:
            Dict with keys: payment_references, evidence_refs,
            financial_resolution, payment_analysis.
        """
        order_id = context.get("order_id", "")  # noqa: F841 — dùng khi gọi MCP
        evidence_refs: list[str] = []
        payment_references: list[str] = []

        # ── Step 1: Fetch payments ───────────────────────────────────
        # TODO (TV4): Gọi MCP tool để lấy thông tin payment
        # Ví dụ:
        #   pay_evidence = await self.call_tool("get_payments", case_id, order_id=order_id)
        #   evidence_refs.append(pay_evidence["evidence_ref"])
        #   payments = pay_evidence["data"]
        #   for p in payments:
        #       payment_references.append(p["payment_sequential"])  # hoặc ID tương ứng

        # ── Step 2: Fetch refunds (nếu có) ───────────────────────────
        # TODO (TV4): Kiểm tra xem có refund nào liên quan không
        # Ví dụ:
        #   refund_evidence = await self.call_tool("get_refunds", case_id, order_id=order_id)
        #   evidence_refs.append(refund_evidence["evidence_ref"])

        # ── Step 3: Analyse ──────────────────────────────────────────
        # TODO (TV4): Phân tích payment
        # - Tổng payment có khớp giá order?
        # - Có duplicate charge?
        # - Split payment có valid?
        # - Refund status: pending / failed / completed?

        # ── Step 4: Build financial_resolution ───────────────────────
        # TODO (TV4): Tính toán refund
        # - recommended_refund_brl: Tổng số tiền cần hoàn
        # - refund_lines: Chi tiết từng dòng hoàn tiền
        #   Ví dụ:
        #   refund_lines = [
        #       {"reason_code": "overpayment", "amount_brl": 50.0, "entity_id": order_id},
        #   ]
        #   recommended_refund_brl = sum(line["amount_brl"] for line in refund_lines)

        financial_resolution = {
            "currency": "BRL",
            "recommended_refund_brl": 0,  # TODO: calculate
            "refund_lines": [],           # TODO: populate
        }

        # ── Handoff ──────────────────────────────────────────────────
        self.emit_handoff(case_id, target="coordinator")

        return {
            "payment_references": list(dict.fromkeys(payment_references)),
            "evidence_refs": evidence_refs,
            "financial_resolution": financial_resolution,
            "payment_analysis": {},  # TODO: populate with analysis details
        }
