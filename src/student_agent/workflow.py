from __future__ import annotations

import asyncio
from typing import Any

from .mcp_gateway import EvidenceGateway
from .specialists import (
    OrderSpecialist,
    PaymentSpecialist,
    PolicySpecialist,
    ShipmentSpecialist,
    VerifierSpecialist,
)
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Autonomous Multi-Agent workflow for investigating e-commerce disputes.

    Architecture:
    Coordinator -> (Parallel Specialists: Order, Payment, Shipment) -> Policy -> Verifier -> Output
    """
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]

    # 1. Initialize specialist agents
    order_agent = OrderSpecialist(gateway, trace)
    payment_agent = PaymentSpecialist(gateway, trace)
    shipment_agent = ShipmentSpecialist(gateway, trace)
    policy_agent = PolicySpecialist(gateway, trace)
    verifier_agent = VerifierSpecialist(trace)

    # 2. Coordinator emits task assignments to domain specialists
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
        decision_code="DISPATCH_ORDER_INVESTIGATION",
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
        decision_code="DISPATCH_PAYMENT_INVESTIGATION",
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
        decision_code="DISPATCH_SHIPMENT_INVESTIGATION",
    )

    # 3. Specialists execute investigation across MCP Evidence Gateway
    order_ctx, payment_ctx, shipment_ctx = await asyncio.gather(
        order_agent.investigate(case_id, order_id),
        payment_agent.investigate(case_id, order_id),
        shipment_agent.investigate(case_id, order_id),
    )

    # 4. Handoff context to Policy Specialist
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="policy-agent",
        decision_code="SPECIALISTS_CONTEXT_AGGREGATED",
    )

    # 5. Policy Specialist formulates resolution based on authoritative policy
    verdict = await policy_agent.evaluate(case, order_ctx, payment_ctx, shipment_ctx)

    # 6. Handoff verdict and contexts to Verifier
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier",
        decision_code="VERDICT_SUBMITTED_FOR_VERIFICATION",
    )

    # 7. Collect all authoritative evidence_refs strictly from this case
    all_case_evidence_refs = (
        order_ctx.evidence_refs
        + payment_ctx.evidence_refs
        + shipment_ctx.evidence_refs
        + verdict.evidence_refs
    )

    # 8. Verifier certifies all invariants and builds final payload
    output = verifier_agent.verify_and_finalize(
        case=case,
        case_id=case_id,
        order_id=order_id,
        order_ctx=order_ctx,
        payment_ctx=payment_ctx,
        shipment_ctx=shipment_ctx,
        verdict=verdict,
        all_case_evidence_refs=all_case_evidence_refs,
    )

    return output
