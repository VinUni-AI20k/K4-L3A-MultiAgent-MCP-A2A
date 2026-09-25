from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseSpecialist, SpecialistResult

SHIPMENT_AGENT_PROMPT = """You are a Logistics and Shipment Specialist in an e-commerce dispute investigation system.
Your job is to analyze delivery timelines, estimated delivery promises, carrier handover events, and SLA violations.
Distinguish clearly whether a late delivery was caused by the seller (dispatching after shipping limit) or by the logistics carrier (delayed delivery after timely dispatch).
Be concise, timeline-driven, and strictly objective."""


class ShipmentSpecialist(BaseSpecialist):
    """Specialist responsible for analyzing shipping timeline, delivery status, and SLA delays."""

    def __init__(self, name: str = "shipment_specialist", llm: BaseChatModel | None = None) -> None:
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
        shipment_id = f"ship_{order_id[:12]}"
        result.entities = {
            "shipment_ids": {shipment_id},
        }

        shipment_ev = await self.safe_call_tool(
            gateway, trace, "get_shipment_summary", case_id, order_id=order_id
        )

        if shipment_ev:
            result.evidence_refs.append(shipment_ev["evidence_ref"])
            sdata = shipment_ev.get("data", {})
            delivered_carrier_at = sdata.get("delivered_carrier_at")
            delivered_customer_at = sdata.get("delivered_customer_at")
            estimated_delivery_at = sdata.get("estimated_delivery_at")
            shipping_limits = sdata.get("shipping_limits", [])
            events = sdata.get("events", [])

            result.facts["delivered_carrier_at"] = delivered_carrier_at
            result.facts["delivered_customer_at"] = delivered_customer_at
            result.facts["estimated_delivery_at"] = estimated_delivery_at
            result.facts["shipping_limits"] = shipping_limits
            result.facts["shipment_events"] = events
            result.facts["is_delivered"] = delivered_customer_at is not None

            # Detect late delivery & responsible party
            is_late = False
            late_party = None

            # Check explicit audit events first
            for ev in events:
                if ev.get("event_type") == "delivered_late":
                    is_late = True
                    actor = ev.get("actor")
                    if actor == "seller":
                        late_party = "seller"
                    elif actor in ("logistics", "logistics_provider", "carrier"):
                        late_party = "logistics_provider"

            # If not explicitly marked in events, compare timestamps
            if not is_late and delivered_customer_at and estimated_delivery_at:
                if delivered_customer_at > estimated_delivery_at:
                    is_late = True
                    seller_dispatched_late = False
                    for limit in shipping_limits:
                        limit_at = limit.get("shipping_limit_at")
                        if limit_at and delivered_carrier_at and delivered_carrier_at > limit_at:
                            seller_dispatched_late = True
                            break
                    late_party = "seller" if seller_dispatched_late else "logistics_provider"

            result.facts["is_late"] = is_late
            result.facts["late_party"] = late_party
        else:
            result.errors.append("Failed to retrieve shipment summary")
            result.facts["is_delivered"] = False
            result.facts["is_late"] = False
            result.facts["late_party"] = None

        # Optional LLM reasoning / evaluation
        if self.llm:
            try:
                user_msg = (
                    f"Customer Context: {customer_context}\n"
                    f"Shipment Facts: {result.facts}\n"
                    "Analyze the logistics timeline. Is the package delivered? Was it delayed, and if so, who is at fault?"
                )
                response = await self.llm.ainvoke([
                    SystemMessage(content=SHIPMENT_AGENT_PROMPT),
                    HumanMessage(content=user_msg),
                ])
                result.reasoning = str(response.content)
            except Exception as exc:
                result.reasoning = f"LLM reasoning skipped: {exc}"

        return result
