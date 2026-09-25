"""Verifier: final quality gate (ARCHITECTURE.md section 6). Never calls MCP and
never edits the candidate; it only accepts or rejects with reason codes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .a2a import (
    POLICY_AGENT,
    VERIFIER,
    AgentContext,
    AgentResult,
    AgentTask,
    RemediationRequest,
    VerificationResult,
    VerificationTaskPayload,
)
from .contracts import ContractError, Contracts

REQUIRED_BEFORE_VERIFY = ("case_received", "task_assigned", "handoff")


class VerifierAgent:
    actor = VERIFIER

    def __init__(self, contracts: Contracts) -> None:
        self._contracts = contracts

    async def handle(self, task: AgentTask, context: AgentContext) -> AgentResult:
        payload = task.payload
        assert isinstance(payload, VerificationTaskPayload)
        reasons, fixable = self.check(task.case_id, payload, context.ledger)
        accepted = not reasons
        remediation: tuple[RemediationRequest, ...] = ()
        if not accepted and fixable and task.attempt == 0:
            remediation = (
                RemediationRequest(
                    target=POLICY_AGENT, reason_code=reasons[0], output_paths=("/",)
                ),
            )
        decision = (
            "ACCEPTED" if accepted else ("REMEDIATION_FAILED" if task.attempt else reasons[0])
        )
        context.trace.emit(
            event_type="verification_completed",
            actor=self.actor,
            decision_code=decision[:80],
            attributes={
                "task_id": task.task_id,
                "attempt": task.attempt,
                "candidate_revision": payload.candidate_revision,
                "accepted": accepted,
                "reason_codes": ",".join(reasons)[:200] or None,
            },
        )
        result = VerificationResult(
            payload.candidate_revision, accepted, tuple(reasons), remediation
        )
        return AgentResult(
            task.local_run_id, task.case_id, task.task_id, self.actor, "completed", result
        )

    def check(
        self, case_id: str, payload: VerificationTaskPayload, ledger: Any
    ) -> tuple[list[str], bool]:
        candidate = payload.candidate_output
        hard: list[str] = []
        soft: list[str] = []
        try:
            self._contracts.validate_output(candidate, "candidate")
        except ContractError:
            return ["UNSCORABLE_SCHEMA"], True
        if candidate.get("case_id") != case_id:
            return ["CASE_ID_MISMATCH"], False

        refs = candidate["evidence_refs"]
        for ref in refs:
            if ledger is not None and ref not in ledger:
                hard.append("UNKNOWN_EVIDENCE_REF")
            elif ledger is not None and ledger.get(ref).case_id != case_id:
                hard.append("CROSS_SCOPE_EVIDENCE_REF")
        for claim in candidate.get("claim_assessments", []):
            if not set(claim["evidence_refs"]) <= set(refs):
                hard.append("CLAIM_REFS_NOT_IN_OUTPUT")
            if claim["verdict"] == "supported" and not claim["evidence_refs"]:
                hard.append("SUPPORTED_WITHOUT_EVIDENCE")
        if len({c["claim_id"] for c in candidate.get("claim_assessments", [])}) != len(
            candidate.get("claim_assessments", [])
        ):
            hard.append("DUPLICATE_CLAIM_ASSESSMENT")

        consumed = {
            ref
            for event in payload.trace_snapshot
            if event.get("event_type") == "tool_result_consumed"
            for ref in event.get("evidence_refs", [])
        }
        if set(refs) - consumed:
            hard.append("EVIDENCE_NOT_CONSUMED")
        seen_types = {event.get("event_type") for event in payload.trace_snapshot}
        for required in REQUIRED_BEFORE_VERIFY:
            if required not in seen_types:
                hard.append(f"TRACE_MISSING_{required.upper()}")

        assessment = candidate["assessment"]
        issue, status = assessment["primary_issue"], assessment["case_status"]
        if issue != "insufficient_evidence" and not refs:
            hard.append("MISSING_REQUIRED_EVIDENCE")

        money = candidate["financial_resolution"]
        lines = money["refund_lines"]
        total = sum((Decimal(str(line["amount_brl"])) for line in lines), Decimal("0"))
        recommended = Decimal(str(money["recommended_refund_brl"]))
        if abs(total - recommended) > Decimal("0.005"):
            hard.append("REFUND_TOTAL_MISMATCH")
        if recommended > 0 and (status != "action_required" or not candidate["resolution_actions"]):
            hard.append("REFUND_WITHOUT_ACTION")
        if status == "no_action" and (
            recommended > 0 or any("refund" in a for a in candidate["resolution_actions"])
        ):
            hard.append("NO_ACTION_WITH_REFUND")
        if len({(line["reason_code"], line["entity_id"]) for line in lines}) != len(lines):
            hard.append("DUPLICATE_REFUND_LINE")

        causes = candidate["root_cause_analysis"]["ranked_causes"]
        if [c["rank"] for c in causes] != list(range(1, len(causes) + 1)):
            hard.append("CAUSE_RANK_INVALID")
        parties = candidate["root_cause_analysis"]["responsible_parties"]
        if len({(p["party_type"], p["party_id"]) for p in parties}) != len(parties):
            hard.append("DUPLICATE_RESPONSIBLE_PARTY")
        types = {p["party_type"] for p in parties}
        if issue == "late_delivery_seller" and "logistics_provider" in types:
            hard.append("RESPONSIBILITY_INCONSISTENT")
        if issue == "late_delivery_logistics" and "seller" in types:
            hard.append("RESPONSIBILITY_INCONSISTENT")
        entity_ids = {v for values in candidate["affected_entities"].values() for v in values}
        for party in parties:
            if (
                party["party_type"] == "seller"
                and party["party_id"]
                and party["party_id"] not in entity_ids
            ):
                soft.append("SELLER_NOT_IN_ENTITIES")
        for conflict in candidate["data_conflicts"]:
            if conflict["selected_source"] not in (None, *conflict["sources"]):
                hard.append("CONFLICT_SOURCE_INVALID")
        if issue == "insufficient_evidence" and status != "needs_investigation":
            hard.append("STATUS_INCONSISTENT")
        reasons = sorted(set(hard + soft))
        fixable = bool(reasons) and not any(
            code in reasons
            for code in ("UNKNOWN_EVIDENCE_REF", "CROSS_SCOPE_EVIDENCE_REF", "CASE_ID_MISMATCH")
        )
        return reasons, fixable
