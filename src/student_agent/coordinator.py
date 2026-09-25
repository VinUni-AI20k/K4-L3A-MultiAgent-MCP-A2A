from __future__ import annotations

import asyncio
from typing import Any

from .a2a import AgentMessage
from .evidence import EvidenceCollector
from .mcp_gateway import EvidenceGateway
from .observability import record_message
from .order_agent import inspect_order
from .state import CaseState, create_case_state
from .trace import TraceWriter
from .payment_agent import inspect_payment
from .shipment_agent import inspect_shipment


async def investigate_order(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> CaseState:
    """Điều phối order, payment và shipment khi claim yêu cầu."""

    state = create_case_state(case)
    topics = {
        claim["topic"]
        for claim in case["customer_request"]["claims"]
    }
    needs_shipment = bool(
        topics & {
            "late_delivery_logistics",
            "late_delivery_seller",
        }
    )
    collector = EvidenceCollector(gateway, state)

    # Bao gồm discovery và xử lý agent trong deadline của case.
    async with asyncio.timeout_at(state.deadline):
        remaining = state.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Case deadline exceeded")

        async with asyncio.timeout(min(30.0, remaining)):
            available_tools = set(await gateway.list_tools())

        required_tools = {"get_order", "get_payment_timeline"}
        if needs_shipment:
            required_tools.add("get_shipment_summary")

        missing_tools = required_tools - available_tools
        if missing_tools:
            raise RuntimeError(
                f"Required MCP tools unavailable: {sorted(missing_tools)}"
            )

        assignment = AgentMessage(
            case_id=state.case_id,
            sender="coordinator",
            recipient="order-item-agent",
            task="Verify order identity and status",
            entity_scope={
                "order_ids": list(state.entity_scope["order_ids"])
            },
            status="pending",
        )
        record_message(state, trace, assignment)

        result = await inspect_order(state, collector, trace)
        record_message(state, trace, result)
        payment_assignment = AgentMessage(
            case_id=state.case_id,
            sender="coordinator",
            recipient="payment-agent",
            task="Verify payment timeline for the scoped order",
            entity_scope={
                "order_ids": list(state.entity_scope["order_ids"])
            },
            status="pending",
        )
        record_message(state, trace, payment_assignment)

        payment_result = await inspect_payment(
            state, collector, trace
        )
        record_message(state, trace, payment_result)

        if needs_shipment:
            shipment_assignment = AgentMessage(
                case_id=state.case_id,
                sender="coordinator",
                recipient="shipment-agent",
                task="Verify shipment timeline and shipping limits",
                entity_scope={
                    "order_ids": list(state.entity_scope["order_ids"])
                },
                status="pending",
            )
            record_message(state, trace, shipment_assignment)

            shipment_result = await inspect_shipment(
                state, collector, trace
            )
            record_message(state, trace, shipment_result)

    return state
