from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .a2a import AgentMessage, VerifiedFact
from .evidence import EvidenceCollector
from .observability import record_evidence_consumed
from .state import CaseState
from .trace import TraceWriter


async def inspect_payment(
    state: CaseState,
    collector: EvidenceCollector,
    trace: TraceWriter,
) -> AgentMessage:
    """Xác minh cấu trúc timeline thanh toán của một đơn."""

    if collector.state is not state:
        raise ValueError("Collector and agent must share case state")

    order_ids = state.entity_scope.get("order_ids", [])
    if len(order_ids) != 1:
        raise ValueError("This agent step requires exactly one order")

    order_id = order_ids[0]
    evidence = await collector.collect(
        "payment-agent",
        "get_payment_timeline",
        order_id=order_id,
    )

    if evidence["domain"] != "payment":
        raise ValueError("Expected payment evidence")

    data = evidence["data"]
    if not isinstance(data, dict):
        raise ValueError("Payment timeline must be an object")

    if data.get("order_id") != order_id:
        raise ValueError("Payment timeline belongs to another order")

    events = data.get("events")
    if not isinstance(events, list):
        raise ValueError("Payment events must be an array")

    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Payment event must be an object")

        if event.get("order_id") != order_id:
            raise ValueError("Payment event belongs to another order")

        for key in ("event_at", "event_type", "status"):
            value = event.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Invalid payment event field: {key}")

        try:
            amount = Decimal(str(event.get("amount_brl")))
        except InvalidOperation as exc:
            raise ValueError("Invalid payment event amount") from exc

        if not amount.is_finite():
            raise ValueError("Payment event amount must be finite")

    evidence_ref = evidence["evidence_ref"]
    fact = VerifiedFact(
        name="payment_timeline_events",
        value=deepcopy(events),
        evidence_refs=[evidence_ref],
    )

    record_evidence_consumed(
        state,
        trace,
        "payment-agent",
        [evidence_ref],
    )

    return AgentMessage(
        case_id=state.case_id,
        sender="payment-agent",
        recipient="coordinator",
        task="Report validated payment timeline events",
        entity_scope={"order_ids": [order_id]},
        facts=[fact],
        evidence_refs=[evidence_ref],
        status="completed",
    )