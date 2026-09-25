from __future__ import annotations

import weakref
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from .agents.shipment_policy import ShipmentPolicyAgent
from .evidence import EvidenceLedger
from .mcp_gateway import EvidenceGateway
from .order_agent import OrderItemAgent
from .payment import run_payment_agent
from .trace import TraceWriter
from .verifier import PRIMARY_ISSUES, SpecialistReport, VerifierAgent

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment_agent"
SHIPMENT_POLICY_AGENT = "shipment_policy_agent"
VERIFIER = "verifier"
MAX_TURNS = 3  # one turn per specialist; the verifier is always the final step


class MessageEnvelope(BaseModel):
    case_id: str
    sender: str
    receiver: str
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs_collected: list[str] = Field(default_factory=list)
    data_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    turn_count: int = 0


class RecordingGateway:
    """Gateway proxy used by specialists: every MCP response lands in the run ledger.

    The ledger is what the verifier trusts, so specialists cannot hand over an
    evidence ref that the MCP gateway did not return for this exact case.
    """

    def __init__(self, gateway: EvidenceGateway, ledger: EvidenceLedger) -> None:
        self._gateway = gateway
        self._ledger = ledger
        self.actor = COORDINATOR

    async def list_tools(self) -> list[str]:
        return await self._gateway.list_tools()

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        response = await self._gateway.call(tool_name, case_id=case_id, **arguments)
        self._ledger.record(
            case_id=case_id, tool_name=tool_name, actor=self.actor, response=response
        )
        return response


# One ledger per run (per TraceWriter) so evidence reuse across cases is detected.
_LEDGERS: weakref.WeakKeyDictionary[TraceWriter, EvidenceLedger] = weakref.WeakKeyDictionary()


def _ledger_for(trace: TraceWriter) -> EvidenceLedger:
    ledger = _LEDGERS.get(trace)
    if ledger is None:
        ledger = _LEDGERS[trace] = EvidenceLedger()
    return ledger


def _collect(envelope: MessageEnvelope, refs: list[str]) -> None:
    for ref in refs:
        if ref not in envelope.evidence_refs_collected:
            envelope.evidence_refs_collected.append(ref)


async def call_order_agent(
    envelope: MessageEnvelope, gateway: EvidenceGateway, trace: TraceWriter
) -> MessageEnvelope:
    """Agent của Dũng"""
    claimed_order_id = envelope.payload.get("customer_request", {}).get("claimed_order_id")
    order_result = await OrderItemAgent(gateway, trace).run(envelope.case_id, claimed_order_id)
    _collect(envelope, order_result.get("evidence_refs", []))
    envelope.payload["order_agent_result"] = order_result
    envelope.sender = ORDER_AGENT
    return envelope


async def call_payment_agent(
    envelope: MessageEnvelope, gateway: EvidenceGateway, trace: TraceWriter
) -> MessageEnvelope:
    """Agent của Long: Chuyên gia điều tra Thanh toán & Dòng tiền"""
    envelope = await run_payment_agent(envelope, gateway, trace)
    envelope.sender = PAYMENT_AGENT
    return envelope


async def call_shipment_policy_agent(
    envelope: MessageEnvelope, gateway: EvidenceGateway, trace: TraceWriter
) -> MessageEnvelope:
    """Agent của Quân"""
    payload = envelope.payload
    customer_request = payload.get("customer_request", {})
    report = await ShipmentPolicyAgent(gateway, trace).investigate(
        case_id=envelope.case_id,
        order_id=customer_request.get("claimed_order_id") or payload.get("order_id") or "",
        claims=customer_request.get("claims") or [],
        policy_version=payload.get("policy_version") or "EC_POLICY_V1",
        order=(payload.get("order_agent_result") or {}).get("order_data"),
        opened_at=payload.get("opened_at"),
    )
    _collect(envelope, report.get("evidence_refs", []))
    envelope.data_conflicts.extend(report.get("data_conflicts") or [])
    envelope.payload["shipment_policy_report"] = report
    envelope.sender = SHIPMENT_POLICY_AGENT
    return envelope


def _proposals(envelope: MessageEnvelope) -> dict[str, str | None]:
    """Issue each specialist claims to have found, when it is inside its own domain."""
    payment = envelope.payload.get("payment_analysis") or {}
    shipment = (envelope.payload.get("shipment_policy_report") or {}).get(
        "policy_recommendation"
    ) or {}
    shipment_issue = shipment.get("primary_issue")
    return {
        ORDER_AGENT: None,
        PAYMENT_AGENT: payment.get("detected_issue"),
        # Its default "unsupported_claim" only means "no shipment problem".
        SHIPMENT_POLICY_AGENT: shipment_issue
        if shipment_issue in ("late_delivery_seller", "late_delivery_logistics")
        else None,
    }


def call_verifier_agent(
    case: dict[str, Any], envelope: MessageEnvelope, ledger: EvidenceLedger, trace: TraceWriter
) -> dict[str, Any]:
    """Thành viên 5: build A2A reports from the ledger and let the verifier decide."""
    by_actor: dict[str, SpecialistReport] = {}
    proposals = _proposals(envelope)
    for evidence in ledger.for_case(envelope.case_id):
        report = by_actor.get(evidence.actor)
        if report is None:
            proposed = proposals.get(evidence.actor)
            report = by_actor[evidence.actor] = SpecialistReport(
                agent=evidence.actor,
                case_id=envelope.case_id,
                proposed_issue=proposed if proposed in PRIMARY_ISSUES else None,
            )
        report.evidence.append(evidence)
    verifier = VerifierAgent(trace.contracts, ledger, trace)
    return verifier.verify(case, list(by_actor.values()))


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: route the case through the specialists, then to the verifier.

    `case_received` and `case_finalized` are emitted by the CLI around this call.
    """
    case_id = case["case_id"]
    ledger = _ledger_for(trace)
    recorder = RecordingGateway(gateway, ledger)
    envelope = MessageEnvelope(
        case_id=case_id, sender=COORDINATOR, receiver=ORDER_AGENT, payload=dict(case)
    )
    plan: list[
        tuple[str, Callable[..., Awaitable[MessageEnvelope]]]
    ] = [
        (ORDER_AGENT, call_order_agent),
        (PAYMENT_AGENT, call_payment_agent),
        (SHIPMENT_POLICY_AGENT, call_shipment_policy_agent),
    ][:MAX_TURNS]

    for actor, _ in plan:
        trace.emit(case_id=case_id, event_type="task_assigned", actor=COORDINATOR, target=actor)

    for actor, run_agent in plan:
        trace.emit(case_id=case_id, event_type="handoff", actor=COORDINATOR, target=actor)
        envelope.receiver = actor
        envelope.turn_count += 1
        recorder.actor = actor
        before = len(ledger.for_case(case_id))
        try:
            envelope = await run_agent(envelope, recorder, trace)
            outcome = "SPECIALIST_DONE"
        except Exception as exc:  # a failing specialist must not sink the whole case
            outcome = "SPECIALIST_FAILED"
            error = f"{type(exc).__name__}: {exc}"[:200]
        else:
            error = None
        refs = [e.evidence_ref for e in ledger.for_case(case_id)[before:]]
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target=COORDINATOR,
            decision_code=outcome,
            evidence_refs=refs[:20] or None,
            attributes={"error": error} if error else None,
        )
    recorder.actor = COORDINATOR

    trace.emit(case_id=case_id, event_type="task_assigned", actor=COORDINATOR, target=VERIFIER)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=COORDINATOR,
        target=VERIFIER,
        attributes={"evidence_count": len(ledger.for_case(case_id))},
    )
    envelope.receiver = VERIFIER
    return call_verifier_agent(case, envelope, ledger, trace)
