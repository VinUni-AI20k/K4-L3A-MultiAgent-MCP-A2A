from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from .a2a import AgentMessage, VerifiedFact
from .evidence import EvidenceCollector
from .observability import record_evidence_consumed
from .state import CaseState
from .trace import TraceWriter


def require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {label}")


def check_timestamp(
    value: object,
    label: str,
    *,
    nullable: bool = False,
) -> None:
    if value is None and nullable:
        return

    require_text(value, label)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")


async def inspect_shipment(
    state: CaseState,
    collector: EvidenceCollector,
    trace: TraceWriter,
) -> AgentMessage:
    """Kiểm tra shipment summary; chưa kết luận trách nhiệm."""

    if collector.state is not state:
        raise ValueError("Collector and agent must share case state")

    order_ids = state.entity_scope.get("order_ids", [])
    if len(order_ids) != 1:
        raise ValueError("This agent step requires exactly one order")

    order_id = order_ids[0]
    evidence = await collector.collect(
        "shipment-agent",
        "get_shipment_summary",
        order_id=order_id,
    )

    if evidence["domain"] != "shipment":
        raise ValueError("Expected shipment evidence")

    data = evidence["data"]
    if not isinstance(data, dict):
        raise ValueError("Shipment data must be an object")

    if data.get("order_id") != order_id:
        raise ValueError("Shipment belongs to another order")

    require_text(data.get("order_status"), "order_status")

    for key in (
        "delivered_carrier_at",
        "delivered_customer_at",
        "estimated_delivery_at",
    ):
        if key not in data:
            raise ValueError(f"Missing shipment field: {key}")
        check_timestamp(data[key], key, nullable=True)

    limits = data.get("shipping_limits")
    if not isinstance(limits, list):
        raise ValueError("shipping_limits must be an array")

    for limit in limits:
        if not isinstance(limit, dict):
            raise ValueError("Shipping limit must be an object")
        require_text(limit.get("order_item_id"), "order_item_id")
        require_text(limit.get("seller_id"), "seller_id")
        check_timestamp(
            limit.get("shipping_limit_at"), "shipping_limit_at"
        )

    events = data.get("events")
    if not isinstance(events, list):
        raise ValueError("Shipment events must be an array")

    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Shipment event must be an object")
        if event.get("order_id") != order_id:
            raise ValueError("Shipment event belongs to another order")
        check_timestamp(event.get("event_at"), "event_at")
        for key in ("event_type", "actor", "status"):
            require_text(event.get(key), key)

    evidence_ref = evidence["evidence_ref"]
    fact = VerifiedFact(
        name="shipment_summary",
        value=deepcopy(data),
        evidence_refs=[evidence_ref],
    )

    record_evidence_consumed(
        state, trace, "shipment-agent", [evidence_ref]
    )

    return AgentMessage(
        case_id=state.case_id,
        sender="shipment-agent",
        recipient="coordinator",
        task="Report validated shipment summary",
        entity_scope={"order_ids": [order_id]},
        facts=[fact],
        evidence_refs=[evidence_ref],
        status="completed",
    )