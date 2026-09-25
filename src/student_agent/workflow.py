from __future__ import annotations

from typing import Any

from .graph import build_case_graph
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run one bounded LangGraph investigation and return its verified output."""
    case_id = str(case["case_id"])
    final_state = await build_case_graph().ainvoke(
        {"case": case, "case_id": case_id, "gateway": gateway, "trace": trace}
    )
    return final_state["output"]
