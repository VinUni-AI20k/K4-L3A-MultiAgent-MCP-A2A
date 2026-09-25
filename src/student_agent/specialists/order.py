from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseSpecialist, SpecialistResult

ORDER_AGENT_PROMPT = """You are an Order and Item Specialist in an e-commerce dispute investigation system.
Your job is to examine the order status, items, prices, and seller information.
Assess if the order was canceled, delivered, or unavailable, and whether the customer claims are consistent with order facts.
Be concise, factual, and strictly objective."""


class OrderSpecialist(BaseSpecialist):
    """Specialist responsible for investigating order status, order items, and sellers."""

    def __init__(self, name: str = "order_specialist", llm: BaseChatModel | None = None) -> None:
        super().__init__(name, llm)

    async def investigate(
        self,
        case_id: str,
        order_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        customer_context: str = "",
    ) -> SpecialistResult:
        result = SpecialistResult(actor=self.name)
        result.entities = {
            "order_ids": {order_id},
            "item_ids": set(),
            "seller_ids": set(),
        }

        # 1. Fetch Order Master
        order_ev = await self.safe_call_tool(gateway, trace, "get_order", case_id, order_id=order_id)
        if order_ev:
            result.evidence_refs.append(order_ev["evidence_ref"])
            order_data = order_ev.get("data", {})
            result.facts["order_id"] = order_data.get("order_id", order_id)
            result.facts["order_status"] = order_data.get("order_status")
            result.facts["customer_id"] = order_data.get("customer_id")
            result.facts["purchase_timestamp"] = order_data.get("order_purchase_timestamp")
            result.facts["approved_at"] = order_data.get("order_approved_at")
            result.facts["delivered_carrier_date"] = order_data.get("order_delivered_carrier_date")
            result.facts["delivered_customer_date"] = order_data.get("order_delivered_customer_date")
            result.facts["estimated_delivery_date"] = order_data.get("order_estimated_delivery_date")
        else:
            result.errors.append("Failed to retrieve order data")

        # 2. Fetch Order Items
        items_ev = await self.safe_call_tool(
            gateway, trace, "get_order_items", case_id, order_id=order_id
        )
        items_list: list[dict[str, Any]] = []
        if items_ev:
            result.evidence_refs.append(items_ev["evidence_ref"])
            raw_items = items_ev.get("data", [])
            if isinstance(raw_items, list):
                items_list = raw_items
            elif isinstance(raw_items, dict):
                items_list = raw_items.get("items", [raw_items])

            total_items_price = 0.0
            total_freight_value = 0.0
            for item in items_list:
                item_id = str(item.get("order_item_id") or "")
                seller_id = str(item.get("seller_id") or "")
                if item_id:
                    result.entities["item_ids"].add(item_id)
                if seller_id:
                    result.entities["seller_ids"].add(seller_id)

                try:
                    total_items_price += float(item.get("price") or 0.0)
                    total_freight_value += float(item.get("freight_value") or 0.0)
                except (ValueError, TypeError):
                    pass

            result.facts["items"] = items_list
            result.facts["total_items_price"] = round(total_items_price, 2)
            result.facts["total_freight_value"] = round(total_freight_value, 2)
        else:
            result.errors.append("Failed to retrieve order items")

        # 3. Fetch Sellers Info
        sellers_ev = await self.safe_call_tool(
            gateway, trace, "get_sellers", case_id, order_id=order_id
        )
        if sellers_ev:
            result.evidence_refs.append(sellers_ev["evidence_ref"])
            sellers_data = sellers_ev.get("data", [])
            if isinstance(sellers_data, list):
                for s in sellers_data:
                    sid = str(s.get("seller_id") or "")
                    if sid:
                        result.entities["seller_ids"].add(sid)
            result.facts["sellers"] = sellers_data

        # 4. Optional LLM reasoning / evaluation
        if self.llm:
            try:
                user_msg = (
                    f"Customer Context: {customer_context}\n"
                    f"Order Facts: {result.facts}\n"
                    "Evaluate if the order status contradicts or supports the customer request."
                )
                response = await self.llm.ainvoke([
                    SystemMessage(content=ORDER_AGENT_PROMPT),
                    HumanMessage(content=user_msg),
                ])
                result.reasoning = str(response.content)
            except Exception as exc:
                result.reasoning = f"LLM reasoning skipped: {exc}"

        return result
