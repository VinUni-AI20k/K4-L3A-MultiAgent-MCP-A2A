from __future__ import annotations

from typing import Any

from .agents.coordinator import CoordinatorAgent
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the L3A multi-agent workflow for a single case.

    Delegates all orchestration to the CoordinatorAgent, which dispatches
    specialist agents (order, payment, shipment, policy) and runs a final
    verification pass before returning the output.
    """
    coordinator = CoordinatorAgent(gateway, trace)
    return await coordinator.run(case["case_id"], case)
