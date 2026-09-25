from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from .a2a import ORDER_AGENT, PAYMENT_AGENT, POLICY_AGENT, SHIPMENT_AGENT, VERIFIER
from .agents import OrderAgent, PaymentAgent, ShipmentAgent
from .contracts import Contracts
from .coordinator import CaseTrace, Coordinator
from .ledger import CaseEvidence, EvidenceLedger
from .mcp_gateway import EvidenceGateway
from .policy import PolicyAgent
from .trace import TraceWriter
from .verifier import VerifierAgent

# One local run id per process (= per `day09 run`); never sent to MCP.
LOCAL_RUN_ID = f"run_{secrets.token_hex(8)}"


def build_coordinator(gateway: EvidenceGateway | None, contracts: Contracts) -> Coordinator:
    agents = {
        ORDER_AGENT: OrderAgent(),
        PAYMENT_AGENT: PaymentAgent(),
        SHIPMENT_AGENT: ShipmentAgent(),
        POLICY_AGENT: PolicyAgent(),
        VERIFIER: VerifierAgent(contracts),
    }

    def context_factory(case_id: str, trace: CaseTrace) -> dict[str, Any]:
        ledger = EvidenceLedger(LOCAL_RUN_ID, case_id)
        evidence = CaseEvidence(gateway, ledger, trace) if gateway is not None else None
        return {"gateway": evidence, "ledger": ledger}

    return Coordinator(
        agents=agents,
        contracts=contracts,
        local_run_id=LOCAL_RUN_ID,
        context_factory=context_factory,
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the coordinator-driven multi-agent workflow for one case.

    Raises ``CaseFailedError`` when no candidate is accepted by the verifier; the CLI
    then writes no output and does not emit ``case_finalized`` for that case.
    """
    contracts = getattr(trace, "contracts", None) or Contracts(
        Path(__file__).resolve().parents[2] / "contracts" / "schemas"
    )
    coordinator = build_coordinator(gateway, contracts)
    return await coordinator.solve(case, trace)
