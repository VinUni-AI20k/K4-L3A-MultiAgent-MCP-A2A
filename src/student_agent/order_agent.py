from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class OrderItemAgent:
    """Agent xác minh thông tin đơn hàng, sản phẩm và người bán.

    Phạm vi: Order, Items, Products, Sellers.
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def run(self, case_id: str, claimed_order_id: str | None) -> dict[str, Any]:
        """Thực hiện điều tra đơn hàng từ MCP Evidence Gateway.

        Args:
            case_id: ID của case khiếu nại (ví dụ: "L3A_CASE_001")
            claimed_order_id: ID đơn hàng khách hàng khai báo trong khiếu nại

        Returns:
            dict chứa thông tin trạng thái đơn, giá trị đơn, danh sách items, sellers,
            các entities thu thập được và danh sách evidence_refs.
        """
        evidence_refs: list[str] = []
        order_data: dict[str, Any] | None = None
        items_data: list[dict[str, Any]] = []
        products_data: list[dict[str, Any]] = []
        
        order_ids: list[str] = []
        item_ids: list[str] = []
        seller_ids: list[str] = []

        if not claimed_order_id:
            return {
                "order_exists": False,
                "order_status": "missing_claimed_order_id",
                "order_data": None,
                "items": [],
                "total_items_price": 0.0,
                "total_freight": 0.0,
                "total_order_value": 0.0,
                "affected_entities": {
                    "order_ids": [],
                    "item_ids": [],
                    "seller_ids": [],
                },
                "evidence_refs": [],
            }

        # 1. Gọi MCP tool: get_order
        try:
            order_resp = await self.gateway.call(
                "get_order",
                case_id=case_id,
                order_id=claimed_order_id,
            )
            ev_ref = order_resp["evidence_ref"]
            evidence_refs.append(ev_ref)
            order_data = order_resp.get("data", {})
            order_ids.append(claimed_order_id)

            # Emit trace event cho tool_result_consumed
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order-agent",
                tool_name="get_order",
                evidence_refs=[ev_ref],
                attributes={
                    "order_id": claimed_order_id,
                    "order_status": order_data.get("order_status") if order_data else None,
                },
            )
        except Exception:
            order_data = None

        # 2. Gọi MCP tool: get_order_items nếu đơn hàng tồn tại
        if order_data is not None:
            try:
                items_resp = await self.gateway.call(
                    "get_order_items",
                    case_id=case_id,
                    order_id=claimed_order_id,
                )
                ev_ref = items_resp["evidence_ref"]
                evidence_refs.append(ev_ref)

                raw_items = items_resp.get("data", [])
                if isinstance(raw_items, list):
                    items_data = raw_items
                elif isinstance(raw_items, dict) and "items" in raw_items:
                    items_data = raw_items["items"]

                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order-agent",
                    tool_name="get_order_items",
                    evidence_refs=[ev_ref],
                    attributes={"items_count": len(items_data)},
                )
            except Exception:
                items_data = []

        # 3. Trích xuất item_id, seller_id
        for item in items_data:
            item_id = str(item.get("order_item_id") or item.get("item_id") or "")
            seller_id = str(item.get("seller_id") or "")

            if item_id and item_id not in item_ids:
                item_ids.append(item_id)
            if seller_id and seller_id not in seller_ids:
                seller_ids.append(seller_id)

        # 4. Gọi MCP tool: get_product_context (tool chỉ nhận order_id, trả về mọi sản phẩm)
        if items_data:
            try:
                prod_resp = await self.gateway.call(
                    "get_product_context",
                    case_id=case_id,
                    order_id=claimed_order_id,
                )
                ev_ref = prod_resp["evidence_ref"]
                evidence_refs.append(ev_ref)
                raw_products = prod_resp.get("data") or []
                products_data = raw_products if isinstance(raw_products, list) else [raw_products]

                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order-agent",
                    tool_name="get_product_context",
                    evidence_refs=[ev_ref],
                    attributes={"products_count": len(products_data)},
                )
            except Exception:
                products_data = []

        # 5. Tính toán tổng tiền hàng và cước vận chuyển
        total_items_price = sum(float(item.get("price", 0.0)) for item in items_data)
        total_freight = sum(float(item.get("freight_value", 0.0)) for item in items_data)

        return {
            "order_exists": order_data is not None,
            "order_id": claimed_order_id,
            "order_status": order_data.get("order_status") if order_data else "not_found",
            "order_data": order_data,
            "items": items_data,
            "products": products_data,
            "total_items_price": round(total_items_price, 2),
            "total_freight": round(total_freight, 2),
            "total_order_value": round(total_items_price + total_freight, 2),
            "affected_entities": {
                "order_ids": order_ids,
                "item_ids": item_ids,
                "seller_ids": seller_ids,
            },
            "evidence_refs": evidence_refs,
        }
