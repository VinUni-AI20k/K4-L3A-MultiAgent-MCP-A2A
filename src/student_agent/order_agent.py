from __future__ import annotations

from .a2a import AgentMessage, VerifiedFact
from .evidence import EvidenceCollector
from .observability import record_evidence_consumed
from .state import CaseState
from .trace import TraceWriter


async def inspect_order(
    state: CaseState,
    collector: EvidenceCollector,
    trace: TraceWriter,
) -> AgentMessage:
    """Xác minh trạng thái một đơn trong phạm vi case."""

    if collector.state is not state:
        raise ValueError("Collector and agent must share case state")

    order_ids = state.entity_scope.get("order_ids", [])
    if len(order_ids) != 1:
        raise ValueError("This agent step requires exactly one order")

    order_id = order_ids[0]

    evidence = await collector.collect(
        "order-item-agent",
        "get_order",
        order_id=order_id,
    )

    if evidence["domain"] != "order":
        raise ValueError("Expected order evidence")

    data = evidence["data"]
    if not isinstance(data, dict):
        raise ValueError("Order data must be an object")

    if data.get("order_id") != order_id:
        raise ValueError("Returned order does not match requested order")

    status = data.get("order_status")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("Missing or invalid order_status")

    evidence_ref = evidence["evidence_ref"]
    fact = VerifiedFact(
        name="order_status",
        value=status,
        evidence_refs=[evidence_ref],
    )

    record_evidence_consumed(
        state,
        trace,
        "order-item-agent",
        [evidence_ref],
    )

    return AgentMessage(
        case_id=state.case_id,
        sender="order-item-agent",
        recipient="coordinator",
        task="Report verified order status",
        entity_scope={"order_ids": [order_id]},
        facts=[fact],
        evidence_refs=[evidence_ref],
        status="completed",
    )