from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .base import BaseSpecialist, SpecialistResult

PAYMENT_AGENT_PROMPT = """You are a Payment and Financial Audit Specialist in an e-commerce dispute investigation system.
Your job is to examine payment transactions, installment breakdowns, duplicate charges, and refund histories.
Determine if the customer was charged multiple times, if a split payment occurred, or if an expected refund failed.
Be concise, accurate with currency (BRL), and strictly objective."""


class PaymentSpecialist(BaseSpecialist):
    """Specialist responsible for inspecting payment records, timelines, and refund status."""

    def __init__(self, name: str = "payment_specialist", llm: BaseChatModel | None = None) -> None:
        super().__init__(name, llm)

    async def investigate(
        self,
        case_id: str,
        order_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        customer_context: str = "",
    ) -> SpecialistResult:
        result = SpecialistResult(actor=self.name)
        result.entities = {
            "payment_references": set(),
        }

        # 1. Fetch Order Payments
        payments_ev = await self.safe_call_tool(
            gateway, trace, "get_order_payments", case_id, order_id=order_id
        )
        payments_list: list[dict[str, Any]] = []
        total_paid_brl = 0.0

        if payments_ev:
            result.evidence_refs.append(payments_ev["evidence_ref"])
            raw_payments = payments_ev.get("data", [])
            if isinstance(raw_payments, list):
                payments_list = raw_payments
            elif isinstance(raw_payments, dict):
                payments_list = raw_payments.get("payments", [raw_payments])

            for idx, p in enumerate(payments_list):
                seq = str(p.get("payment_sequential", idx + 1))
                ptype = str(p.get("payment_type", "unknown"))
                ref = f"pay_{order_id[:8]}_{ptype}_{seq}"
                result.entities["payment_references"].add(ref)

                try:
                    val = float(p.get("payment_value") or 0.0)
                    total_paid_brl += val
                except (ValueError, TypeError):
                    pass

            result.facts["payments"] = payments_list
            result.facts["total_paid_brl"] = round(total_paid_brl, 2)
            result.facts["payment_count"] = len(payments_list)
            result.facts["is_split_payment"] = len(payments_list) > 1
        else:
            result.errors.append("Failed to retrieve payment data")

        # 2. Fetch Payment Timeline & Check Duplicates
        timeline_ev = await self.safe_call_tool(
            gateway, trace, "get_payment_timeline", case_id, order_id=order_id
        )
        if timeline_ev:
            result.evidence_refs.append(timeline_ev["evidence_ref"])
            tdata = timeline_ev.get("data", {})
            events = tdata.get("events", []) if isinstance(tdata, dict) else []
            result.facts["payment_events"] = events

            # Check duplicate charge: two captured events with identical amount
            amounts = [e.get("amount_brl") for e in events if e.get("event_type") == "captured"]
            result.facts["is_duplicate_charge"] = len(amounts) > 1 and len(amounts) != len(set(amounts))
        else:
            result.facts["payment_events"] = []
            result.facts["is_duplicate_charge"] = False

        # 3. Fetch Refund Timeline (Optional)
        refund_ev = await self.safe_call_tool(
            gateway, trace, "get_refund_timeline", case_id, order_id=order_id
        )
        if refund_ev:
            result.evidence_refs.append(refund_ev["evidence_ref"])
            rdata = refund_ev.get("data", {})
            result.facts["refund_data"] = rdata
            result.facts["has_existing_refund"] = True
        else:
            result.facts["refund_data"] = None
            result.facts["has_existing_refund"] = False

        # 4. Optional LLM reasoning / evaluation
        if self.llm:
            try:
                user_msg = (
                    f"Customer Context: {customer_context}\n"
                    f"Payment Facts: {result.facts}\n"
                    "Analyze the financial transactions. Is there a duplicate charge, valid split payment, or refund failure?"
                )
                response = await self.llm.ainvoke([
                    SystemMessage(content=PAYMENT_AGENT_PROMPT),
                    HumanMessage(content=user_msg),
                ])
                result.reasoning = str(response.content)
            except Exception as exc:
                result.reasoning = f"LLM reasoning skipped: {exc}"

        return result
