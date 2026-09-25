from __future__ import annotations

from typing import Any

from .agents import Coordinator
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_COORDINATOR = Coordinator()


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Entry point wired to the CLI: routes the case through the L3A multi-agent
    workflow (Coordinator -> specialists -> Policy Agent -> Verifier Agent).

    See ARCHITECTURE.md for the full design and src/student_agent/agents.py
    for the agent implementations.
    """
    return await _COORDINATOR.solve(case, gateway, trace)
