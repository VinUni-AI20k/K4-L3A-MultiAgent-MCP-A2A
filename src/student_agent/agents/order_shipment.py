from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway, call_with_retry
from ..trace import TraceWriter


class OrderShipmentAgent:
    """Collect authoritative order, item, seller and shipment evidence."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter):
        self.gateway = gateway
        self.trace = trace

    async def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Fetch tools whose arguments are scoped to the claimed order."""
        order_id = context.get("claimed_order_id")
        if not isinstance(order_id, str) or not order_id:
            return {"evidence": {}, "data": {}, "errors": ["missing claimed order id"]}

        tool_names = (
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_sellers",
        )
        evidence: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for tool_name in tool_names:
            try:
                result = await call_with_retry(
                    self.gateway,
                    tool_name,
                    case_id=case_id,
                    order_id=order_id,
                )
            except Exception as error:
                errors.append(f"{tool_name}: {type(error).__name__}")
                continue
            evidence[tool_name] = result
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order_shipment_agent",
                tool_name=tool_name,
                evidence_refs=[result["evidence_ref"]],
                attributes={"domain": result["domain"]},
            )

        return {
            "evidence": evidence,
            "data": {name: value["data"] for name, value in evidence.items()},
            "errors": errors,
        }
