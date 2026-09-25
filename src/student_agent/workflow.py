from __future__ import annotations

from typing import Any

from .agents.coordinator import CoordinatorAgent
from .agents.order_shipment import OrderShipmentAgent
from .agents.payment_policy import PaymentPolicyAgent
from .agents.verifier import VerifierAgent
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the coordinator, specialists and verifier for one case."""
    case_id = case["case_id"]
    coordinator = CoordinatorAgent(gateway, trace)
    order_shipment = OrderShipmentAgent(gateway, trace)
    payment_policy = PaymentPolicyAgent(gateway, trace)
    verifier = VerifierAgent(gateway, trace)

    context = await coordinator.run(case)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="order_shipment_agent",
        attributes={"scope": "order_item_seller_shipment"},
    )
    context["fulfillment"] = await order_shipment.run(case_id, context)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="payment_policy_agent",
        attributes={"scope": "payment_refund_policy"},
    )
    context["finance"] = await payment_policy.run(case_id, context)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        attributes={"scope": "contract_and_consistency_check"},
    )
    return verifier.run(case_id, context)
