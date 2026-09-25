"""Coordinator agent — orchestrates the multi-agent workflow.

Owner: Thành viên 1 (feat/tv1-coordinator-trace)

Ghép code thật của cả nhóm qua adapter — KHÔNG sửa file của thành viên khác:

1. OrderAgent (TV2)      ``run(case, state_dict)``
2. ShipmentAgent (TV3)   ``investigate(case_id, order_id, seller_ids, opened_at)``
3. PaymentAgent (TV4)    ``investigate(case_id=, order_id=, gateway=, trace=, ...)``
4. Gọi bù tool cốt lõi mà các agent trên chưa gọi đúng tham số (dưới tên actor sở hữu tool)
5. Evidence rules (TV1) + tín hiệu của specialist → Adjudicator chọn primary_issue
6. PolicyAgent (TV5)     ``load`` + ``decide`` → case_status / action / refund / parties
7. VerifierAgent (TV5)   ``verify(state, draft, trace)``
8. Output guard (TV1)    bảo vệ cuối + đảm bảo mọi ref có trong trace

Mỗi agent của thành viên khác đi qua ``RecordingGateway`` nên mọi evidence đều được ghi
vào ledger của case. ``case_received`` / ``case_finalized`` do ``cli.py`` emit.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

from ..contracts import ContractError
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from . import adjudicator, evidence_rules
from .base import BaseAgent, EvidenceLedger, RecordingGateway, TraceTap
from .guard import SCHEMA_VERSION, enforce_invariants, fallback_output
from .order_agent import OrderAgent
from .payment_agent import PaymentAgent
from .policy_agent import PolicyAgent
from .shipment_agent import ShipmentAgent
from .state import CaseState
from .state import EvidenceRecord as StateEvidence
from .verifier_agent import VerifierAgent

# Tool cốt lõi → actor sở hữu (theo bảng phân công). Tham số luôn là order_id.
CORE_TOOLS: dict[str, str] = {
    "get_order": "order-agent",
    "get_order_items": "order-agent",
    "get_shipment_summary": "shipment-agent",
    "get_sellers": "shipment-agent",
    "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
}
SPECIALIST_STRENGTH = 0.5  # tín hiệu từ agent đồng đội: tham khảo, evidence rules quyết định


class _ToolRunner(BaseAgent):
    """Gọi bù một tool cốt lõi dưới tên actor sở hữu tool đó."""


class CoordinatorAgent:
    """Top-level orchestrator — NOT a BaseAgent subclass (it doesn't call MCP tools directly)."""

    name = "coordinator"

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace
        self.tap = TraceTap(trace)
        self.ledger: EvidenceLedger | None = None
        self.policy_agent = PolicyAgent()
        self.verifier_agent = VerifierAgent(self.policy_agent)

    async def run(self, case_id: str, case: dict[str, Any]) -> dict[str, Any]:
        """Orchestrate the full workflow for a single case."""
        self.ledger = ledger = EvidenceLedger(case_id)
        tap = self.tap
        request = case.get("customer_request") or {}
        claims = request.get("claims") or []
        order_id = request.get("claimed_order_id") or ""

        # ── 1. Order (TV2) ───────────────────────────────────────────
        order_state: dict[str, Any] = {}
        order_agent = OrderAgent(self._gw("order-agent"), tap)
        await self._task("order-agent", case_id, lambda: order_agent.run(case, order_state))
        self._handoff("order-agent", case_id, "ORDER_INVESTIGATED")
        order_data = order_state.get("order_data") or {}

        # ── 2. Shipment (TV3) ────────────────────────────────────────
        shipment_agent = ShipmentAgent(self._gw("shipment-agent"), tap)
        shipment = await self._task(
            "shipment-agent",
            case_id,
            lambda: shipment_agent.investigate(
                case_id, order_id, [], case.get("opened_at")
            ),
        )

        # ── 3. Payment (TV4) ─────────────────────────────────────────
        payment_agent = PaymentAgent()
        payment = await self._task(
            "payment-agent",
            case_id,
            lambda: payment_agent.investigate(
                case_id=case_id,
                order_id=order_id,
                gateway=self._gw("payment-agent"),
                trace=tap,
                order_status=order_data.get("order_status"),
            ),
        )
        self._handoff("payment-agent", case_id, "PAYMENT_INVESTIGATED")

        # ── 4. Gọi bù tool cốt lõi còn thiếu ─────────────────────────
        if order_id:
            await self._fill_gaps(case_id, order_id)

        # ── 5. Evidence rules + adjudication ─────────────────────────
        facts = evidence_rules.extract_facts(case, ledger)
        signals = {
            "evidence-rules": {"issues": evidence_rules.derive_signals(facts)},
            "order-agent": {"issues": self._order_signals(order_state, claims)},
            "shipment-agent": {"issues": self._issue_signal(shipment, "primary_issue_candidate")},
            "payment-agent": {"issues": self._issue_signal(payment, "detected_issue")},
        }
        decision = adjudicator.decide(claims, signals, ledger)

        # ── 6. Policy (TV5) ──────────────────────────────────────────
        state = CaseState.from_case(case)
        self._sync_state(state)
        policy_decision = None
        try:
            await self._task(
                "policy-agent",
                case_id,
                lambda: self.policy_agent.load(state, self._gw("policy-agent"), tap),
            )
            policy_decision = self.policy_agent.decide(state, decision.primary_issue, tap)
            policy_decision = self._localize_parties(policy_decision, facts)
            state.policy_decision = policy_decision
            decision.case_status = policy_decision.case_status
        except Exception as exc:  # noqa: BLE001 — policy lỗi vẫn phải ra output
            self._handoff("policy-agent", case_id, "agent_error", error=type(exc).__name__)
        self._sync_state(state)
        decision.evidence_refs = adjudicator.select_evidence(
            decision.primary_issue, decision.case_status, ledger, decision.evidence_refs
        )
        self._emit_decision(case_id, decision)

        # ── 7. Draft → Verifier (TV5) ────────────────────────────────
        draft = self._draft(case, facts, decision, policy_decision)
        state.primary_issue_candidate = decision.primary_issue
        self.tap.emit(
            case_id=case_id, event_type="task_assigned", actor=self.name, target="verifier"
        )
        try:
            verified = self.verifier_agent.verify(state, draft, tap)
        except Exception as exc:  # noqa: BLE001 — verifier lỗi → guard xử lý draft
            self._handoff("verifier", case_id, "agent_error", error=type(exc).__name__)
            verified = draft
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
        self._link_evidence(case_id, final)
        self.tap.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="output-guard",
            target=self.name,
            decision_code="fixed" if fixes else "passed",
            evidence_refs=final["evidence_refs"][:20] or None,
            attributes={"fix_count": len(fixes), "fixes": ",".join(fixes)[:200] or None},
        )
        return final

    # ------------------------------------------------------------------
    # Adapters & helpers
    # ------------------------------------------------------------------

    def _gw(self, actor: str) -> RecordingGateway:
        assert self.ledger is not None
        return RecordingGateway(self.gateway, self.ledger, actor)

    async def _task(
        self, actor: str, case_id: str, work: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Giao việc cho một agent; lỗi của agent không được làm hỏng cả case."""
        self.tap.emit(case_id=case_id, event_type="task_assigned", actor=self.name, target=actor)
        try:
            return await work()
        except Exception as exc:  # noqa: BLE001 — cô lập lỗi specialist
            self._handoff(actor, case_id, "agent_error", error=type(exc).__name__)
            return None

    def _handoff(self, actor: str, case_id: str, code: str, **attrs: Any) -> None:
        self.tap.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target=self.name,
            decision_code=code,
            attributes=attrs or None,
        )

    async def _fill_gaps(self, case_id: str, order_id: str) -> None:
        assert self.ledger is not None
        done = set(self.ledger.by_tool())
        for tool, actor in CORE_TOOLS.items():
            key = (tool, (("order_id", order_id),))
            if tool in done or key in self.ledger.attempted:
                continue
            runner = _ToolRunner(actor, self.gateway, self.tap)
            runner.ledger = self.ledger
            try:
                await runner.call_tool(tool, case_id, order_id=order_id)
            except Exception:  # noqa: BLE001 — vd refund timeline không tồn tại
                continue

    @staticmethod
    def _order_signals(
        order_state: dict[str, Any], claims: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        topics = {c.get("claim_id"): c.get("topic") for c in claims}
        signals = []
        for item in order_state.get("claim_assessments") or []:
            topic = topics.get(item.get("claim_id")) or ""
            if item.get("verdict") == "supported" and topic in adjudicator.PRIMARY_ISSUES:
                signals.append(BaseAgent.signal(topic, SPECIALIST_STRENGTH, []))
        return signals

    @staticmethod
    def _issue_signal(result: Any, attr: str) -> list[dict[str, Any]]:
        issue = getattr(result, attr, None) if result is not None else None
        if issue not in adjudicator.PRIMARY_ISSUES:
            return []
        refs = list(getattr(result, "evidence_refs", None) or [])
        return [BaseAgent.signal(issue, SPECIALIST_STRENGTH, refs)]

    def _sync_state(self, state: CaseState) -> None:
        """Đồng bộ ledger ↔ CaseState của TV5 (verifier chỉ tin ref có trong CaseState)."""
        assert self.ledger is not None
        for ref, record in self.ledger.records.items():
            if ref not in state.evidence:
                state.evidence[ref] = StateEvidence(
                    ref=ref,
                    domain=record.domain,
                    tool_name=record.tool,
                    actor=record.agent,
                    case_id=state.case_id,
                    warnings=record.warnings,
                )
                state.evidence_refs.append(ref)

    @staticmethod
    def _localize_parties(decision: Any, facts: evidence_rules.CaseFacts) -> Any:
        """Policy là chung cho mọi case: seller_id trong rule chỉ là ví dụ → thay bằng
        seller thật của case (lấy từ evidence trong cửa sổ thời gian)."""
        parties: list[dict[str, Any]] = []
        for party in decision.responsible_parties:
            if party["party_type"] == "seller" and facts.seller_ids:
                parties += [{"party_type": "seller", "party_id": s} for s in facts.seller_ids]
            else:
                parties.append(dict(party))
        unique = list({(p["party_type"], p["party_id"]): p for p in parties}.values())[:5]
        return dataclasses.replace(decision, responsible_parties=tuple(unique))

    def _draft(
        self,
        case: dict[str, Any],
        facts: evidence_rules.CaseFacts,
        decision: adjudicator.Decision,
        policy: Any,
    ) -> dict[str, Any]:
        order_ids = [facts.order_id] if facts.order_id else []
        refund = float(policy.refund_brl) if policy is not None else 0.0
        lines = (
            [{"reason_code": decision.primary_issue, "amount_brl": refund,
              "entity_id": order_ids[0] if order_ids else None}]
            if refund > 0 else []
        )
        claims = (case.get("customer_request") or {}).get("claims") or []
        return {
            "schema_version": SCHEMA_VERSION,
            "case_id": case["case_id"],
            "assessment": decision.assessment(),
            "affected_entities": {
                "order_ids": order_ids,
                "item_ids": facts.item_ids,
                "seller_ids": facts.seller_ids,
                "payment_references": [],
                "shipment_ids": [],
            },
            "claim_assessments": adjudicator.default_claim_assessments(claims, decision, refund),
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": decision.primary_issue.upper(), "rank": 1}],
                "responsible_parties": [dict(p) for p in policy.responsible_parties]
                if policy is not None else [],
            },
            "evidence_refs": decision.evidence_refs,
            "data_conflicts": evidence_rules.stale_conflicts(facts),
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": refund,
                "refund_lines": lines,
            },
            "resolution_actions": [policy.recommended_action] if policy is not None else [],
        }

    def _link_evidence(self, case_id: str, output: dict[str, Any]) -> None:
        """Mọi ref trong output phải có tool_result_consumed trong trace của case."""
        assert self.ledger is not None or not output["evidence_refs"]
        refs = list(output["evidence_refs"])
        for claim in output.get("claim_assessments") or []:
            refs += claim["evidence_refs"]
        for ref in dict.fromkeys(refs):
            if ref in self.tap.consumed or self.ledger is None or ref not in self.ledger:
                continue
            record = self.ledger.records[ref]
            self.tap.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=record.agent,
                tool_name=record.tool,
                evidence_refs=[ref],
            )

    def _emit_decision(self, case_id: str, decision: adjudicator.Decision) -> None:
        top = ",".join(f"{issue}:{score}" for issue, score in decision.candidates[:3])
        self.tap.emit(
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
