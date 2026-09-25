"""Shipment / Logistics specialist agent.

Owner: Thành viên 3 (feat/tv3-logistics-specialist)

Responsibilities:
- Fetch shipment and seller details via MCP
- Determine delivery issues: late delivery, who is responsible
- Populate shipment_ids and seller_ids for affected_entities
- Provide responsible_parties for root_cause_analysis
- Collect evidence_refs from MCP calls

MCP Tools to discover (run ``day09 mcp-tools``):
- get_shipment / get_shipments (or similar)
- get_seller (or similar)
"""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseAgent


class ShipmentAgent(BaseAgent):
    """Specialist agent for shipment and logistics data gathering."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        super().__init__(name="shipment-agent", gateway=gateway, trace=trace)

    async def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Fetch and analyse shipment + seller data.

        Args:
            case_id: The case identifier.
            context: Must contain ``order_id``.

        Returns:
            Dict with keys: shipment_ids, seller_ids, evidence_refs,
            delivery_analysis, responsible_parties.
        """
        order_id = context.get("order_id", "")  # noqa: F841 — dùng khi gọi MCP
        evidence_refs: list[str] = []
        shipment_ids: list[str] = []
        seller_ids: list[str] = []

        # ── Step 1: Fetch shipment(s) ────────────────────────────────
        # TODO (TV3): Gọi MCP tool để lấy thông tin vận chuyển
        # Ví dụ:
        #   ship_evidence = await self.call_tool("get_shipment", case_id, order_id=order_id)
        #   evidence_refs.append(ship_evidence["evidence_ref"])
        #   shipment_data = ship_evidence["data"]
        #   shipment_ids.append(shipment_data.get("shipment_id", ""))

        # ── Step 2: Fetch seller(s) ──────────────────────────────────
        # TODO (TV3): Gọi MCP tool để lấy thông tin người bán
        # Ví dụ (sẽ cần seller_id từ order/item data):
        #   seller_evidence = await self.call_tool("get_seller", case_id, seller_id=seller_id)
        #   evidence_refs.append(seller_evidence["evidence_ref"])
        #   seller_ids.append(seller_id)

        # ── Step 3: Analyse delivery ─────────────────────────────────
        # TODO (TV3): Phân tích giao hàng
        # - So sánh estimated_delivery_date vs actual_delivery_date
        # - Xác định trễ do seller hay do logistics:
        #   + shipping_limit_date: deadline seller phải giao cho carrier
        #   + Nếu seller giao muộn hơn shipping_limit_date → late_delivery_seller
        #   + Nếu seller giao đúng hạn nhưng carrier trễ → late_delivery_logistics
        is_late = False         # TODO: determine
        late_party = "unknown"  # TODO: "seller" or "logistics_provider"

        # ── Step 4: Build responsible_parties ─────────────────────────
        # TODO (TV3): Xác định bên chịu trách nhiệm
        # responsible_parties = [
        #     {"party_type": "seller", "party_id": seller_id},
        # ]
        responsible_parties: list[dict[str, Any]] = []

        # ── Handoff ──────────────────────────────────────────────────
        self.emit_handoff(case_id, target="coordinator")

        return {
            "shipment_ids": list(dict.fromkeys(shipment_ids)),
            "seller_ids": list(dict.fromkeys(seller_ids)),
            "evidence_refs": evidence_refs,
            "delivery_analysis": {
                "is_late": is_late,
                "late_party": late_party,
            },
            "responsible_parties": responsible_parties,
        }
