from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import OrderLogisticsResult
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


class OrderLogisticsAgent:
    """Agent phụ trách điều tra Đơn hàng, Sản phẩm và Giao vận (Thành viên 2)."""

    def __init__(self, actor_name: str = "order-logistics-agent") -> None:
        self.actor_name = actor_name

    async def investigate(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> OrderLogisticsResult:
        case_id = case["case_id"]
        customer_req = case.get("customer_request", {})
        order_id = customer_req.get("claimed_order_id") or ""

        result = OrderLogisticsResult(
            order_id=order_id,
            order_status="unknown",
            order_ids=[order_id] if order_id else [],
        )

        if not order_id:
            return result

        # 1. Gọi MCP get_order
        try:
            order_evidence = await gateway.call("get_order", case_id=case_id, order_id=order_id)
            ev_ref = order_evidence.get("evidence_ref")
            if ev_ref:
                result.evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor_name,
                    tool_name="get_order",
                    evidence_refs=[ev_ref],
                )

            order_data = order_evidence.get("data", {})
            result.order_data = order_data
            result.order_status = order_data.get("order_status", "unknown").lower()
        except Exception as exc:
            logger.warning(f"[{case_id}] get_order error: {exc}")

        # 2. Gọi MCP get_order_items (để lấy danh sách items, sellers, giá tiền)
        try:
            items_evidence = await gateway.call(
                "get_order_items", case_id=case_id, order_id=order_id
            )
            ev_ref = items_evidence.get("evidence_ref")
            if ev_ref:
                result.evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor_name,
                    tool_name="get_order_items",
                    evidence_refs=[ev_ref],
                )

            raw_items = items_evidence.get("data", [])
            items = raw_items if isinstance(raw_items, list) else raw_items.get("items", [])
            result.items_data = items

            for item in items:
                if item_id := item.get("order_item_id"):
                    result.item_ids.append(str(item_id))
                if seller_id := item.get("seller_id"):
                    result.seller_ids.append(str(seller_id))
                price = float(item.get("price", 0.0))
                freight = float(item.get("freight_value", 0.0))
                result.items_total_brl += price
                result.freight_total_brl += freight

            result.items_total_brl = round(result.items_total_brl, 2)
            result.freight_total_brl = round(result.freight_total_brl, 2)
            result.order_total_brl = round(result.items_total_brl + result.freight_total_brl, 2)

        except Exception as exc:
            logger.info(f"[{case_id}] get_order_items error: {exc}")

        # 3. Gọi MCP get_shipment_summary
        try:
            shipment_evidence = await gateway.call(
                "get_shipment_summary", case_id=case_id, order_id=order_id
            )
            ev_ref = shipment_evidence.get("evidence_ref")
            if ev_ref:
                result.evidence_refs.append(ev_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor_name,
                    tool_name="get_shipment_summary",
                    evidence_refs=[ev_ref],
                )

            shipment_data = shipment_evidence.get("data", {})
            result.shipment_data = shipment_data
            if s_id := shipment_data.get("shipment_id"):
                result.shipment_ids.append(str(s_id))

            # Phân tích mốc thời gian giao hàng
            self._analyze_delivery_timeline(result, shipment_data)

        except Exception as exc:
            logger.info(f"[{case_id}] get_shipment_summary not available or error: {exc}")

        # Loại bỏ trùng lặp ID
        result.order_ids = sorted(list(set(result.order_ids)))
        result.item_ids = sorted(list(set(result.item_ids)))
        result.seller_ids = sorted(list(set(result.seller_ids)))
        result.shipment_ids = sorted(list(set(result.shipment_ids)))
        result.evidence_refs = list(dict.fromkeys(result.evidence_refs))

        return result

    @staticmethod
    def _parse_dt(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    def _analyze_delivery_timeline(
        self, result: OrderLogisticsResult, shipment_data: dict[str, Any]
    ) -> None:
        """Phân định trễ hạn do bên bán (seller) hay đơn vị giao nhận (logistics_provider)."""
        events = shipment_data.get("events", [])
        for ev in events:
            if ev.get("event_type") == "delivered_late":
                result.is_late = True
                actor = ev.get("actor")
                if actor == "seller":
                    result.delay_party = "seller"
                    result.suggested_issue = "late_delivery_seller"
                    return
                elif actor in ["carrier", "logistics", "logistics_provider"]:
                    result.delay_party = "logistics_provider"
                    result.suggested_issue = "late_delivery_logistics"
                    return

        # So sánh ngày giờ nếu không có event explicit
        delivered_customer = shipment_data.get("delivered_customer_at") or shipment_data.get(
            "delivered_customer_date"
        )
        estimated_delivery = shipment_data.get("estimated_delivery_at") or shipment_data.get(
            "estimated_delivery_date"
        )
        delivered_carrier = shipment_data.get("delivered_carrier_at") or shipment_data.get(
            "delivered_carrier_date"
        )

        # Lấy hạn giao sớm nhất của các items
        shipping_limits = shipment_data.get("shipping_limits", [])
        earliest_limit: str | None = None
        for sl in shipping_limits:
            limit_at = sl.get("shipping_limit_at")
            if limit_at and (earliest_limit is None or limit_at < earliest_limit):
                earliest_limit = limit_at

        dt_delivered = self._parse_dt(delivered_customer)
        dt_estimated = self._parse_dt(estimated_delivery)
        dt_carrier = self._parse_dt(delivered_carrier)
        dt_limit = self._parse_dt(earliest_limit)

        if dt_delivered and dt_estimated and dt_delivered > dt_estimated:
            result.is_late = True
            if dt_carrier and dt_limit and dt_carrier > dt_limit:
                result.delay_party = "seller"
                result.suggested_issue = "late_delivery_seller"
            else:
                result.delay_party = "logistics_provider"
                result.suggested_issue = "late_delivery_logistics"
