from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseSpecialist, SpecialistResult

POLICY_AGENT_PROMPT = """You are the Lead Policy and Adjudication Specialist in an e-commerce dispute investigation system.
You receive:
1. Customer complaint claims
2. Objective facts from Order Specialist, Payment Specialist, and Shipment Specialist
3. Platform authoritative rules table from get_policy (EC_POLICY_V1)

Your responsibility is to:
- Identify the exact primary_issue from the allowed issues:
  [canceled_order_paid, unavailable_order_paid, late_delivery_seller, late_delivery_logistics,
   valid_split_payment, payment_mismatch, duplicate_charge, refund_pending, refund_failed,
   unsupported_claim, insufficient_evidence]
- Adjudicate each customer claim as "supported", "unsupported", or "partially_supported"
- Determine the responsible party based on the policy rules table
- Determine the refund amount in BRL and recommended actions
Be rigorous, objective, and adhere strictly to the policy rules table."""


class PolicySpecialist(BaseSpecialist):
    """Specialist that synthesizes specialist findings and applies platform policy rules."""

    def __init__(self, name: str = "policy_specialist", llm: BaseChatModel | None = None) -> None:
        super().__init__(name, llm)

    async def adjudicate(
        self,
        case_id: str,
        policy_version: str,
        customer_request: dict[str, Any],
        order_res: SpecialistResult,
        payment_res: SpecialistResult,
        shipment_res: SpecialistResult,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        """Adjudicate case and formulate final assessment and financial resolution."""

        # 1. Fetch Authoritative Policy Rules from MCP
        policy_ev = await self.safe_call_tool(
            gateway, trace, "get_policy", case_id, policy_version=policy_version
        )
        policy_ref = policy_ev.get("evidence_ref") if policy_ev else None
        policy_data = policy_ev.get("data", {}) if policy_ev else {}
        rules = policy_data.get("rules", {})

        order_facts = order_res.facts
        payment_facts = payment_res.facts
        shipment_facts = shipment_res.facts

        order_status = order_facts.get("order_status")
        is_canceled = order_status == "canceled"
        is_unavailable = order_status == "unavailable"
        total_paid = payment_facts.get("total_paid_brl", 0.0)
        is_duplicate = payment_facts.get("is_duplicate_charge", False)
        is_split = payment_facts.get("is_split_payment", False)
        is_late = shipment_facts.get("is_late", False)
        late_party = shipment_facts.get("late_party")

        # 2. Heuristic baseline classification (matches policy rule keys)
        primary_issue = "unsupported_claim"

        if is_canceled and total_paid > 0:
            primary_issue = "canceled_order_paid"
        elif is_unavailable and total_paid > 0:
            primary_issue = "unavailable_order_paid"
        elif is_duplicate:
            primary_issue = "duplicate_charge"
        elif is_late:
            if late_party == "seller":
                primary_issue = "late_delivery_seller"
            else:
                primary_issue = "late_delivery_logistics"
        elif is_split and not is_canceled and not is_late:
            primary_issue = "valid_split_payment"

        # 3. Lookup rule details from MCP policy data
        selected_rule = rules.get(primary_issue, {})
        case_status = selected_rule.get("case_status", "no_action")
        recommended_action = selected_rule.get("recommended_action", "document_no_action")
        refund_brl = float(selected_rule.get("refund_brl", 0.0))
        responsible_parties = selected_rule.get("responsible_parties", [])

        # Update seller ID in responsible parties if seller is responsible
        if primary_issue == "late_delivery_seller":
            sellers = list(order_res.entities.get("seller_ids", []))
            seller_id = sellers[0] if sellers else None
            responsible_parties = [{"party_type": "seller", "party_id": seller_id}]

        # 4. Claim Assessments
        claims = customer_request.get("claims", [])
        claim_assessments = []
        for c in claims:
            cid = c.get("claim_id", "")
            topic = c.get("topic", "")

            is_match = (topic == primary_issue) or (
                topic == "requested_full_refund" and refund_brl > 0
            )
            verdict = "supported" if is_match else "unsupported"
            c_refs = []
            if order_res.evidence_refs:
                c_refs.append(order_res.evidence_refs[0])
            if policy_ref:
                c_refs.append(policy_ref)

            claim_assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": 0.95 if verdict == "supported" else 0.85,
                "evidence_refs": sorted(list(set(c_refs))),
            })

        # 5. Format Root Cause & Financial Resolution
        cause_code = primary_issue.upper()
        root_cause = {
            "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
            "responsible_parties": responsible_parties,
        }

        claimed_order_id = customer_request.get("claimed_order_id")
        refund_lines = []
        if refund_brl > 0:
            refund_lines.append({
                "reason_code": f"REFUND_{cause_code}",
                "amount_brl": round(refund_brl, 2),
                "entity_id": claimed_order_id,
            })

        financial_resolution = {
            "currency": "BRL",
            "recommended_refund_brl": round(refund_brl, 2),
            "refund_lines": refund_lines,
        }

        # 6. LLM Evaluation (if LLM is configured)
        llm_reasoning = ""
        if self.llm:
            try:
                prompt_text = (
                    f"Customer Request: {customer_request}\n"
                    f"Order Facts: {order_facts}\n"
                    f"Payment Facts: {payment_facts}\n"
                    f"Shipment Facts: {shipment_facts}\n"
                    f"Determined Issue: {primary_issue}\n"
                    f"Policy Rule: {selected_rule}\n"
                    "Confirm if the primary issue and financial resolution align with policy. Provide a 2-sentence rationale."
                )
                response = await self.llm.ainvoke([
                    SystemMessage(content=POLICY_AGENT_PROMPT),
                    HumanMessage(content=prompt_text),
                ])
                llm_reasoning = str(response.content)
            except Exception as exc:
                llm_reasoning = f"LLM skipped: {exc}"

        # Emit policy_decided trace event
        if trace:
            trace.emit(
                case_id=case_id,
                event_type="policy_decided",
                actor=self.name,
                decision_code=f"ISSUE_{cause_code}",
                evidence_refs=[policy_ref] if policy_ref else None,
                attributes={"primary_issue": primary_issue, "refund_brl": refund_brl},
            )

        return {
            "assessment": {
                "primary_issue": primary_issue,
                "case_status": case_status,
                "confidence": 0.95,
            },
            "claim_assessments": claim_assessments,
            "root_cause_analysis": root_cause,
            "financial_resolution": financial_resolution,
            "resolution_actions": [recommended_action],
            "policy_evidence_ref": policy_ref,
            "reasoning": llm_reasoning,
        }

    async def investigate(
        self,
        case_id: str,
        order_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        customer_context: str = "",
    ) -> SpecialistResult:
        # Implemented for polymorphism
        return SpecialistResult(actor=self.name)
