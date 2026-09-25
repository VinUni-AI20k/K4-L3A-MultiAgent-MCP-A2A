"""Coordinator agent — orchestrates the multi-agent workflow.

Owner: Thành viên 1 (feat/tv1-coordinator-trace)

Luồng cho mỗi case:
1. Order agent (lấy order/item → seller_ids cho các agent sau)
2. Payment agent, Shipment agent
3. Adjudicator chọn primary_issue / case_status / evidence (emit policy_decided)
4. Policy agent nhận quyết định → resolution_actions (+ root cause nếu có)
5. Ghép draft → Verifier (TV5) → Guard (bảo vệ cuối) → validate schema

``case_received`` / ``case_finalized`` do ``cli.py`` emit — KHÔNG emit ở đây.
Specialist lỗi không làm hỏng case: coordinator ghi nhận và chạy tiếp với dữ liệu còn lại.
"""

from __future__ import annotations

from typing import Any

from ..contracts import ContractError
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from . import adjudicator
from .base import BaseAgent, EvidenceLedger
from .guard import SCHEMA_VERSION, enforce_invariants, fallback_output, refund_total
from .order_agent import OrderAgent
from .payment_agent import PaymentAgent
from .policy_agent import PolicyAgent
from .shipment_agent import ShipmentAgent
from .verifier_agent import VerifierAgent


class CoordinatorAgent:
    """Top-level orchestrator — NOT a BaseAgent subclass (it doesn't call MCP tools directly)."""

    name = "coordinator"

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace
        self.ledger: EvidenceLedger | None = None

        # Instantiate specialist agents
        self.order_agent = OrderAgent(gateway, trace)
        self.payment_agent = PaymentAgent(gateway, trace)
        self.shipment_agent = ShipmentAgent(gateway, trace)
        self.policy_agent = PolicyAgent(gateway, trace)
        self.verifier_agent = VerifierAgent(gateway, trace)
        self._agents: list[BaseAgent] = [
            self.order_agent,
            self.payment_agent,
            self.shipment_agent,
            self.policy_agent,
            self.verifier_agent,
        ]

    async def run(self, case_id: str, case: dict[str, Any]) -> dict[str, Any]:
        """Orchestrate the full workflow for a single case."""
        self.ledger = EvidenceLedger(case_id)
        for agent in self._agents:
            agent.ledger = self.ledger

        customer_request = case.get("customer_request") or {}
        claims = customer_request.get("claims") or []
        context: dict[str, Any] = {
            "case_id": case_id,
            "opened_at": case.get("opened_at", ""),
            "order_id": customer_request.get("claimed_order_id", ""),
            "claims": claims,
            "claim_topics": [c.get("topic", "") for c in claims],
            "customer_message": customer_request.get("message", ""),
            "policy_version": case.get("policy_version", ""),
        }

        # ── Phase 1: Order trước — các agent sau cần seller_ids / item_ids ──
        order_result = await self._dispatch(self.order_agent, case_id, context)
        context["order_result"] = order_result
        context["seller_ids"] = order_result.get("seller_ids", [])
        context["item_ids"] = order_result.get("item_ids", [])

        # ── Phase 2: Payment + Shipment ──────────────────────────────
        payment_result = await self._dispatch(self.payment_agent, case_id, context)
        context["payment_result"] = payment_result
        shipment_result = await self._dispatch(self.shipment_agent, case_id, context)
        context["shipment_result"] = shipment_result

        specialist_results = {
            self.order_agent.name: order_result,
            self.payment_agent.name: payment_result,
            self.shipment_agent.name: shipment_result,
        }

        # ── Phase 3: Adjudication ────────────────────────────────────
        decision = adjudicator.decide(claims, specialist_results, self.ledger)
        context["decision"] = decision.assessment()

        # ── Phase 4: Policy (biết quyết định → đề xuất hành động) ─────
        policy_result = await self._dispatch(self.policy_agent, case_id, context)
        if decision.source == "no_signal":
            # Specialist chưa phát tín hiệu → cho phép policy agent quyết định
            # (tương thích với cách chia việc cũ, nơi policy agent chọn issue).
            decision = adjudicator.decide(
                claims,
                {**specialist_results, self.policy_agent.name: policy_result},
                self.ledger,
                fallback_assessment=policy_result.get("assessment"),
            )
        self._emit_decision(case_id, decision)

        # ── Phase 5: Draft ───────────────────────────────────────────
        all_results = [order_result, payment_result, shipment_result, policy_result]
        financial = payment_result.get("financial_resolution") or {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        }
        draft_output: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "assessment": decision.assessment(),
            "affected_entities": self._merge_entities(all_results),
            "claim_assessments": order_result.get("claim_assessments")
            or adjudicator.default_claim_assessments(
                claims, decision, refund_total(financial)
            ),
            "root_cause_analysis": self._root_cause(shipment_result, policy_result),
            "evidence_refs": decision.evidence_refs,
            "data_conflicts": [c for r in all_results for c in r.get("data_conflicts") or []],
            "financial_resolution": financial,
            "resolution_actions": policy_result.get("resolution_actions") or [],
        }

        # ── Phase 6: Verifier (TV5) → Guard (TV1) ────────────────────
        verified = await self._dispatch(
            self.verifier_agent, case_id, {**context, "draft_output": draft_output}
        )
        if not verified.get("assessment"):  # verifier lỗi → dùng draft
            verified = draft_output
        return self.finalize(case, verified)

    # ------------------------------------------------------------------
    # Final guard — cũng được workflow.solve_case dùng cho fallback
    # ------------------------------------------------------------------

    def finalize(self, case: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        case_id = case["case_id"]
        ledger = self.ledger or EvidenceLedger(case_id)
        final, fixes = enforce_invariants(output, case, ledger)
        try:
            self.trace.contracts.validate_output(final, f"outputs/{case_id}.json")
        except ContractError as exc:
            fixes.append(f"schema:{str(exc)[-60:]}")
            final = fallback_output(case, ledger)
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="output-guard",
            decision_code="fixed" if fixes else "passed",
            evidence_refs=final["evidence_refs"][:20] or None,
            attributes={"fix_count": len(fixes), "fixes": ",".join(fixes)[:200] or None},
        )
        return final

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _dispatch(
        self, agent: BaseAgent, case_id: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Giao việc cho một agent; lỗi của agent không được làm hỏng cả case."""
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor=self.name,
            target=agent.name,
        )
        try:
            result = await agent.run(case_id, context)
        except Exception as exc:  # noqa: BLE001 — cô lập lỗi specialist
            self.trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=agent.name,
                target=self.name,
                decision_code="agent_error",
                attributes={"error": type(exc).__name__},
            )
            return {"errors": [type(exc).__name__]}
        return result if isinstance(result, dict) else {}

    def _emit_decision(self, case_id: str, decision: adjudicator.Decision) -> None:
        top = ",".join(f"{issue}:{score}" for issue, score in decision.candidates[:3])
        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="adjudicator",
            decision_code=decision.primary_issue,
            evidence_refs=decision.evidence_refs[:20] or None,
            attributes={
                "case_status": decision.case_status,
                "confidence": decision.confidence,
                "source": decision.source,
                "candidates": top[:200] or None,
            },
        )

    @staticmethod
    def _merge_entities(results: list[dict[str, Any]]) -> dict[str, list[str]]:
        keys = ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
        merged: dict[str, list[str]] = {}
        for key in keys:
            values = [v for r in results for v in r.get(key) or []]
            entities = [e for r in results for e in (r.get("entities") or {}).get(key) or []]
            merged[key] = list(dict.fromkeys(str(v) for v in values + entities if v))
        return merged

    @staticmethod
    def _root_cause(
        shipment_result: dict[str, Any], policy_result: dict[str, Any]
    ) -> dict[str, Any]:
        """Ưu tiên RCA của shipment agent (TV3), sau đó policy agent."""
        for result in (shipment_result, policy_result):
            rca = result.get("root_cause_analysis")
            if rca and (rca.get("ranked_causes") or rca.get("responsible_parties")):
                return rca
        return {
            "ranked_causes": [],
            "responsible_parties": shipment_result.get("responsible_parties") or [],
        }
