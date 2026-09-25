"""Order / Item specialist agent.

Owner: Thành viên 2 (feat/tv2-order-specialist)

Responsibilities:
- Fetch order details and item list via MCP
- Determine order status and identify order-level issues
- Populate order_ids, item_ids, and seller_ids for affected_entities
- Collect evidence_refs from MCP calls

MCP Tools to discover (run ``day09 mcp-tools``):
- get_order (or similar)
- get_items / get_order_items (or similar)
"""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseAgent


class OrderAgent(BaseAgent):
    """Specialist agent for order and item data gathering."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        super().__init__(name="order-agent", gateway=gateway, trace=trace)

    async def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Fetch and analyse order + item data.

        Args:
            case_id: The case identifier.
            context: Must contain ``order_id``.

        Returns:
            Dict with keys: order_ids, item_ids, seller_ids, evidence_refs,
            order_data (raw), order_status.
        """
        order_id = context.get("order_id", "")  # noqa: F841 — dùng khi gọi MCP
        evidence_refs: list[str] = []
        order_ids: list[str] = []
        item_ids: list[str] = []
        seller_ids: list[str] = []

        # ── Step 1: Fetch order ──────────────────────────────────────
        # TODO (TV2): Gọi MCP tool để lấy thông tin order
        # Ví dụ:
        #   order_evidence = await self.call_tool("get_order", case_id, order_id=order_id)
        #   evidence_refs.append(order_evidence["evidence_ref"])
        #   order_data = order_evidence["data"]
        #   order_ids.append(order_id)

        # ── Step 2: Fetch items ──────────────────────────────────────
        # TODO (TV2): Gọi MCP tool để lấy danh sách items
        # Ví dụ:
        #   items_evidence = await self.call_tool("get_items", case_id, order_id=order_id)
        #   evidence_refs.append(items_evidence["evidence_ref"])
        #   items_data = items_evidence["data"]
        #   for item in items_data:
        #       item_ids.append(item["item_id"])
        #       if item.get("seller_id"):
        #           seller_ids.append(item["seller_id"])

        # ── Step 3: Analyse ──────────────────────────────────────────
        # TODO (TV2): Phân tích trạng thái order
        # - Order bị cancel?
        # - Có unavailable items?
        # - Trạng thái hiện tại?
        order_status = "unknown"  # TODO: determine from order_data

        # ── Handoff ──────────────────────────────────────────────────
        self.emit_handoff(case_id, target="coordinator")

        return {
            "order_ids": list(dict.fromkeys(order_ids)),
            "item_ids": list(dict.fromkeys(item_ids)),
            "seller_ids": list(dict.fromkeys(seller_ids)),
            "evidence_refs": evidence_refs,
            "order_status": order_status,
            "order_data": {},  # TODO: populate with actual data
        }
