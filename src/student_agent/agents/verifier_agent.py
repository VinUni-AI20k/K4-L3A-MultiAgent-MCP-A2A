"""Verifier agent — validates the draft output before finalization.

Owner: Thành viên 5 (feat/tv5-verifier-policy)

Responsibilities:
- Validate all evidence_refs format
- Check case_id consistency
- Verify financial totals (recommended_refund_brl == sum of refund_lines)
- Ensure affected_entities are populated when case_status is action_required
- Calibrate confidence
- Emit verification_completed trace event
"""

from __future__ import annotations

import copy
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseAgent


class VerifierAgent(BaseAgent):
    """Verification agent — final quality gate before output submission."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        super().__init__(name="verifier", gateway=gateway, trace=trace)

    async def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Verify and optionally fix the draft output.

        Args:
            case_id: The case identifier.
            context: Must contain ``draft_output``.

        Returns:
            The verified (and possibly corrected) output dict.
        """
        output = copy.deepcopy(context["draft_output"])

        # ── Check 1: case_id match ───────────────────────────────────
        if output.get("case_id") != case_id:
            output["case_id"] = case_id

        # ── Check 2: evidence_refs format ────────────────────────────
        # TODO (TV5): Validate tất cả evidence_refs đều match pattern ^ev_[A-Za-z0-9_-]{20,96}$
        # Loại bỏ refs không hợp lệ

        # ── Check 3: financial consistency ───────────────────────────
        # TODO (TV5): recommended_refund_brl phải == sum(refund_lines[].amount_brl)
        fin = output.get("financial_resolution", {})
        refund_lines = fin.get("refund_lines", [])
        total = round(sum(line.get("amount_brl", 0) for line in refund_lines), 2)
        if fin.get("recommended_refund_brl") != total:
            fin["recommended_refund_brl"] = total

        # ── Check 4: affected_entities ───────────────────────────────
        # TODO (TV5): Nếu case_status == "action_required", đảm bảo
        # ít nhất 1 entity set không trống

        # ── Check 5: confidence bounds ───────────────────────────────
        assessment = output.get("assessment", {})
        confidence = assessment.get("confidence", 0.5)
        assessment["confidence"] = max(0.0, min(1.0, confidence))

        # ── Check 6: data_conflicts ──────────────────────────────────
        # TODO (TV5): Phát hiện mâu thuẫn dữ liệu giữa các sources
        # Ví dụ: payment amount != order total

        # ── Emit verification_completed ──────────────────────────────
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.name,
            attributes={"checks_passed": True},  # TODO: report actual status
        )

        self.emit_handoff(case_id, target="coordinator")

        return output
