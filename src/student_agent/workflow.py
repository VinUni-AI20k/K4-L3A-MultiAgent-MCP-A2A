from __future__ import annotations

from typing import Any

from .agents.coordinator import CoordinatorAgent
from .agents.guard import fallback_output
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the L3A multi-agent workflow for a single case.

    Delegates all orchestration to the CoordinatorAgent, which dispatches
    specialist agents (order, payment, shipment, policy) and runs a final
    verification pass before returning the output.

    Never raises for a single broken case: ``cli.py`` would abort the whole batch.
    On failure the case falls back to ``insufficient_evidence`` built only from
    evidence actually retrieved for this case.
    """
    coordinator = CoordinatorAgent(gateway, trace)
    try:
        return await coordinator.run(case["case_id"], case)
    except Exception as exc:  # noqa: BLE001 — cô lập lỗi theo case
        trace.emit(
            case_id=case["case_id"],
            event_type="handoff",
            actor="coordinator",
            target="output-guard",
            decision_code="workflow_error",
            attributes={"error": type(exc).__name__},
        )
        return coordinator.finalize(case, fallback_output(case, coordinator.ledger))
