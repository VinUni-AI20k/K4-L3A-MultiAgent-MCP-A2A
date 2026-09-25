from __future__ import annotations

from typing import Any

from .agents import CoordinatorAgent, Verifier
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Hàm giải quyết case L3A phối hợp Multi-Agent."""
    verifier = Verifier(trace.contracts)
    coordinator = CoordinatorAgent(verifier=verifier)
    return await coordinator.coordinate(case, gateway, trace)
