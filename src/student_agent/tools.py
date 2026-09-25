from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@dataclass
class ToolExecutionContext:
    """Tracks evidence references and audit events when LLMs invoke tools."""

    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter | None = None
    actor: str = "specialist"
    collected_refs: list[str] = field(default_factory=list)


def create_mcp_langchain_tools(context: ToolExecutionContext) -> dict[str, StructuredTool]:
    """Build LangChain StructuredTools hooked into the MCP Evidence Gateway."""

    async def _call_mcp(tool_name: str, **kwargs: Any) -> str:
        try:
            evidence = await context.gateway.call(
                tool_name,
                case_id=context.case_id,
                **kwargs,
            )
            ev_ref = evidence.get("evidence_ref")
            if ev_ref:
                context.collected_refs.append(ev_ref)
                if context.trace:
                    context.trace.emit(
                        case_id=context.case_id,
                        event_type="tool_result_consumed",
                        actor=context.actor,
                        tool_name=tool_name,
                        evidence_refs=[ev_ref],
                    )
            return json.dumps(evidence.get("data", {}), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": f"Tool {tool_name} failed: {exc}"})

    # Tool schemas
    class OrderIdInput(BaseModel):
        order_id: str = Field(description="The authoritative order ID")

    class PolicyInput(BaseModel):
        policy_version: str = Field(description="The platform policy version, e.g. EC_POLICY_V1")

    class CustomerInput(BaseModel):
        customer_unique_id: str = Field(description="The unique customer identifier")

    return {
        "get_order": StructuredTool.from_function(
            coroutine=lambda order_id: _call_mcp("get_order", order_id=order_id),
            name="get_order",
            description="Return the authoritative order row (status, timestamps, customer_id).",
            args_schema=OrderIdInput,
        ),
        "get_order_items": StructuredTool.from_function(
            coroutine=lambda order_id: _call_mcp("get_order_items", order_id=order_id),
            name="get_order_items",
            description="Return items in this order, item prices, freight values, seller IDs, and shipping limits.",
            args_schema=OrderIdInput,
        ),
        "get_sellers": StructuredTool.from_function(
            coroutine=lambda order_id: _call_mcp("get_sellers", order_id=order_id),
            name="get_sellers",
            description="Return seller information (city, state, prefix) for the order.",
            args_schema=OrderIdInput,
        ),
        "get_order_payments": StructuredTool.from_function(
            coroutine=lambda order_id: _call_mcp("get_order_payments", order_id=order_id),
            name="get_order_payments",
            description="Return payment records (types, values, installments, payment_sequential).",
            args_schema=OrderIdInput,
        ),
        "get_payment_timeline": StructuredTool.from_function(
            coroutine=lambda order_id: _call_mcp("get_payment_timeline", order_id=order_id),
            name="get_payment_timeline",
            description="Return payment lifecycle events (captured, authorized, timestamps).",
            args_schema=OrderIdInput,
        ),
        "get_refund_timeline": StructuredTool.from_function(
            coroutine=lambda order_id: _call_mcp("get_refund_timeline", order_id=order_id),
            name="get_refund_timeline",
            description="Return refund lifecycle events and status, if any.",
            args_schema=OrderIdInput,
        ),
        "get_shipment_summary": StructuredTool.from_function(
            coroutine=lambda order_id: _call_mcp("get_shipment_summary", order_id=order_id),
            name="get_shipment_summary",
            description="Return shipment progress, carrier delivery date, customer delivery date, estimated delivery, and SLA events.",
            args_schema=OrderIdInput,
        ),
        "get_policy": StructuredTool.from_function(
            coroutine=lambda policy_version: _call_mcp("get_policy", policy_version=policy_version),
            name="get_policy",
            description="Return platform rules table with issue statuses, refund amounts, and responsible parties.",
            args_schema=PolicyInput,
        ),
        "get_customer_history": StructuredTool.from_function(
            coroutine=lambda customer_unique_id: _call_mcp("get_customer_history", customer_unique_id=customer_unique_id),
            name="get_customer_history",
            description="Return customer purchase history and past dispute behavior.",
            args_schema=CustomerInput,
        ),
    }
