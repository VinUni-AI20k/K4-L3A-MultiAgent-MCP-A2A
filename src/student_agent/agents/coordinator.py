"""Coordinator agent — orchestrates the multi-agent workflow.

Owner: Thành viên 1 (feat/tv1-coordinator-trace)

The coordinator:
1. Parses the incoming case
2. Dispatches specialist agents (order, payment, shipment)
3. Sends aggregated findings to the policy agent
4. Passes the draft output through the verifier
5. Assembles the final L3A output dict
"""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .order_agent import OrderAgent
from .payment_agent import PaymentAgent
from .policy_agent import PolicyAgent
from .shipment_agent import ShipmentAgent
from .verifier_agent import VerifierAgent


class CoordinatorAgent:
    """Top-level orchestrator — NOT a BaseAgent subclass (it doesn't call MCP tools directly)."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

        # Instantiate specialist agents
        self.order_agent = OrderAgent(gateway, trace)
        self.payment_agent = PaymentAgent(gateway, trace)
        self.shipment_agent = ShipmentAgent(gateway, trace)
        self.policy_agent = PolicyAgent(gateway, trace)
        self.verifier_agent = VerifierAgent(gateway, trace)

    async def run(self, case_id: str, case: dict[str, Any]) -> dict[str, Any]:
        """Orchestrate the full workflow for a single case."""
        customer_request = case.get("customer_request", {})
        order_id = customer_request.get("claimed_order_id", "")
        claims = customer_request.get("claims", [])
        policy_version = case.get("policy_version", "")

        # Shared context passed to each specialist
        context: dict[str, Any] = {
            "order_id": order_id,
            "claims": claims,
            "customer_message": customer_request.get("message", ""),
            "policy_version": policy_version,
        }

        # ── Phase 1: Data gathering — dispatch specialists ────────────

        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order-agent",
        )
        order_result = await self.order_agent.run(case_id, context)

        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
        )
        payment_result = await self.payment_agent.run(case_id, context)

        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
        )
        shipment_result = await self.shipment_agent.run(case_id, context)

        # ── Phase 2: Policy analysis ─────────────────────────────────
        context["order_result"] = order_result
        context["payment_result"] = payment_result
        context["shipment_result"] = shipment_result

        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="policy-agent",
        )
        policy_result = await self.policy_agent.run(case_id, context)

        # ── Phase 3: Assemble draft output ────────────────────────────
        all_evidence_refs = list(dict.fromkeys(
            order_result.get("evidence_refs", [])
            + payment_result.get("evidence_refs", [])
            + shipment_result.get("evidence_refs", [])
            + policy_result.get("evidence_refs", [])
        ))

        draft_output: dict[str, Any] = {
            "schema_version": "day09-l3a-output-v2",
            "case_id": case_id,
            "assessment": policy_result.get("assessment", {
                "primary_issue": "insufficient_evidence",
                "case_status": "needs_investigation",
                "confidence": 0.0,
            }),
            "affected_entities": {
                "order_ids": order_result.get("order_ids", []),
                "item_ids": order_result.get("item_ids", []),
                "seller_ids": shipment_result.get("seller_ids", []),
                "payment_references": payment_result.get("payment_references", []),
                "shipment_ids": shipment_result.get("shipment_ids", []),
            },
            "claim_assessments": self._build_claim_assessments(claims, context),
            "root_cause_analysis": policy_result.get("root_cause_analysis", {
                "ranked_causes": [],
                "responsible_parties": [],
            }),
            "evidence_refs": all_evidence_refs,
            "data_conflicts": self._detect_conflicts(context),
            "financial_resolution": payment_result.get("financial_resolution", {
                "currency": "BRL",
                "recommended_refund_brl": 0,
                "refund_lines": [],
            }),
            "resolution_actions": policy_result.get("resolution_actions", []),
        }

        # ── Phase 4: Verification ─────────────────────────────────────
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="verifier",
        )
        verified_output = await self.verifier_agent.run(case_id, {
            "draft_output": draft_output,
            **context,
        })

        return verified_output

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_claim_assessments(
        self, claims: list[dict], context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Build claim_assessments from input claims and gathered evidence.

        TODO (TV2): Implement proper claim evaluation logic based on
        order/payment/shipment results. For now returns a basic skeleton.
        """
        assessments = []
        for claim in claims:
            assessments.append({
                "claim_id": claim["claim_id"],
                "verdict": "insufficient_evidence",  # TODO: evaluate properly
                "confidence": 0.5,
                "evidence_refs": [],  # TODO: link relevant evidence
            })
        return assessments

    def _detect_conflicts(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Detect data conflicts between sources.

        TODO (TV5): Compare data from different specialist results
        and flag mismatches (e.g. order amount vs payment amount).
        """
        return []  # TODO: implement conflict detection
