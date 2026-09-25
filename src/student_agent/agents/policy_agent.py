"""Policy specialist agent.

Owner: Thành viên 5 (feat/tv5-verifier-policy)

Responsibilities:
- Fetch platform policy via MCP
- Determine primary_issue from the 11 possible values
- Determine case_status: action_required / no_action / needs_investigation
- Build root_cause_analysis (ranked_causes + responsible_parties)
- Propose resolution_actions
- Emit policy_decided trace event
"""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseAgent


class PolicyAgent(BaseAgent):
    """Specialist agent for policy evaluation and decision making."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        super().__init__(name="policy-agent", gateway=gateway, trace=trace)

    async def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Evaluate gathered evidence against platform policies.

        Args:
            case_id: The case identifier.
            context: Must contain order_result, payment_result, shipment_result.

        Returns:
            Dict with keys: assessment, root_cause_analysis, resolution_actions,
            evidence_refs.
        """
        evidence_refs: list[str] = []
        policy_version = context.get("policy_version", "")  # noqa: F841 — dùng khi gọi MCP

        # ── Step 1: Fetch policy ─────────────────────────────────────
        # TODO (TV5): Gọi MCP tool để lấy chính sách
        # Ví dụ:
        #   policy_evidence = await self.call_tool(
        #       "get_policy", case_id, policy_version=policy_version
        #   )
        #   evidence_refs.append(policy_evidence["evidence_ref"])
        #   policy_data = policy_evidence["data"]

        # ── Step 2: Determine primary_issue ──────────────────────────
        # TODO (TV5): Dựa vào order/payment/shipment results, xác định issue
        # Các giá trị hợp lệ:
        #   canceled_order_paid, unavailable_order_paid,
        #   late_delivery_seller, late_delivery_logistics,
        #   valid_split_payment, payment_mismatch,
        #   duplicate_charge, refund_pending, refund_failed,
        #   unsupported_claim, insufficient_evidence
        primary_issue = "insufficient_evidence"  # TODO: determine

        # ── Step 3: Determine case_status ────────────────────────────
        # TODO (TV5): action_required / no_action / needs_investigation
        case_status = "needs_investigation"  # TODO: determine

        # ── Step 4: Build root_cause_analysis ────────────────────────
        # TODO (TV5): Xác định nguyên nhân gốc rễ
        # ranked_causes: list of {"cause_code": "LATE_SHIPPING", "rank": 1}
        # responsible_parties: lấy từ shipment_result hoặc tự xác định
        root_cause_analysis = {
            "ranked_causes": [],       # TODO: populate
            "responsible_parties": [],  # TODO: populate
        }

        # ── Step 5: Propose resolution_actions ───────────────────────
        # TODO (TV5): Đề xuất hành động (max 8, mỗi action max 80 chars)
        resolution_actions: list[str] = []  # TODO: populate

        # ── Emit policy_decided ──────────────────────────────────────
        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=self.name,
            decision_code=primary_issue,
        )

        # ── Handoff ──────────────────────────────────────────────────
        self.emit_handoff(case_id, target="coordinator")

        return {
            "assessment": {
                "primary_issue": primary_issue,
                "case_status": case_status,
                "confidence": 0.5,  # TODO: calibrate
            },
            "root_cause_analysis": root_cause_analysis,
            "resolution_actions": resolution_actions,
            "evidence_refs": evidence_refs,
        }
