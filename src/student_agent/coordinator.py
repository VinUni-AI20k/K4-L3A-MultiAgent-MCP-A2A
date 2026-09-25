"""Coordinator / supervisor (ARCHITECTURE.md sections 1, 3, 5, 7.2).

The coordinator owns the lifecycle of one case: intake, routing, task budget, handoff
processing, result validation, aggregation, candidate construction, one remediation
round and the finalize/failed decision. It never calls MCP and never creates business
facts or evidence; every fact reaches it through a validated ``AgentResult``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .a2a import (
    COORDINATOR,
    DOMAIN_AGENTS,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    RESULT_STATUSES,
    SHIPMENT_AGENT,
    TASK_TYPE_BY_AGENT,
    VERIFIER,
    Agent,
    AgentContext,
    AgentResult,
    AgentTask,
    Claim,
    DataConflict,
    DomainResult,
    DomainTaskPayload,
    EntityIds,
    Finding,
    PolicyDecision,
    PolicyTaskPayload,
    VerificationResult,
    VerificationTaskPayload,
)
from .contracts import ContractError, Contracts
from .mcp_gateway import GatewayFatalError

EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
TASK_DEADLINE_SECONDS = 120.0
INITIAL_DOMAIN_BUDGET = 6
REMEDIATION_DOMAIN_BUDGET = 3
PRIORITY = {ORDER_AGENT: 0, PAYMENT_AGENT: 1, SHIPMENT_AGENT: 2, POLICY_AGENT: 3, VERIFIER: 4}
DOMAIN_TO_AGENT = {
    "order": ORDER_AGENT,
    "item": ORDER_AGENT,
    "seller": ORDER_AGENT,
    "product": ORDER_AGENT,
    "payment": PAYMENT_AGENT,
    "refund": PAYMENT_AGENT,
    "shipment": SHIPMENT_AGENT,
}

# Claim topic (customer statement, unverified) -> domains worth investigating.
TOPIC_ROUTES: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": (ORDER_AGENT, PAYMENT_AGENT),
    "unavailable_order_paid": (ORDER_AGENT, PAYMENT_AGENT),
    "late_delivery_seller": (ORDER_AGENT, SHIPMENT_AGENT),
    "late_delivery_logistics": (ORDER_AGENT, SHIPMENT_AGENT),
    "valid_split_payment": (ORDER_AGENT, PAYMENT_AGENT),
    "payment_mismatch": (ORDER_AGENT, PAYMENT_AGENT),
    "duplicate_charge": (ORDER_AGENT, PAYMENT_AGENT),
    "refund_pending": (ORDER_AGENT, PAYMENT_AGENT),
    "refund_failed": (ORDER_AGENT, PAYMENT_AGENT),
    "requested_full_refund": (ORDER_AGENT, PAYMENT_AGENT),
}
# Free-text fallback when a claim carries no known topic (vi/pt/en keywords).
TEXT_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("canceled_order_paid", re.compile(r"cancel|h[uủ]y|huỷ")),
    ("unavailable_order_paid", re.compile(r"unavailable|indispon|h[eế]t h[aà]ng|esgotad")),
    ("late_delivery_logistics", re.compile(r"late|delay|atras|giao nh[aậ]n|tr[eễ]|ch[aậ]m")),
    ("duplicate_charge", re.compile(r"twice|duplicat|duas vezes|hai l[aầ]n|tr[uù]ng")),
    ("payment_mismatch", re.compile(r"thanh to[aá]n|payment|pagamento|charge|cobr")),
    ("refund_pending", re.compile(r"refund|reembols|estorn|ho[aà]n ti[eề]n")),
)


class CaseFailedError(RuntimeError):
    """The case cannot be finalized; no output is written for it."""

    def __init__(self, case_id: str, reason_code: str, detail: str = "") -> None:
        super().__init__(f"{case_id}: {reason_code}{' - ' + detail if detail else ''}")
        self.case_id = case_id
        self.reason_code = reason_code


class CaseTrace:
    """Case-scoped trace facade that also keeps the snapshot handed to the verifier."""

    def __init__(self, writer: Any, case_id: str) -> None:
        self._writer = writer
        self.case_id = case_id
        self.events: list[dict[str, Any]] = self._existing_events()

    def _existing_events(self) -> list[dict[str, Any]]:
        path = getattr(self._writer, "path", None)
        if not isinstance(path, Path) or not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                if event.get("case_id") == self.case_id:
                    events.append(event)
        return events

    def emit(self, *, case_id: str | None = None, **fields: Any) -> dict[str, Any]:
        if case_id is not None and case_id != self.case_id:
            raise ValueError(f"trace event for {case_id} emitted in scope {self.case_id}")
        event = self._writer.emit(case_id=self.case_id, **fields)
        self.events.append(event)
        return event

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(event) for event in self.events)


@dataclass(frozen=True)
class CaseIntake:
    case_id: str
    claims: tuple[Claim, ...]
    claim_topics: Mapping[str, str]
    lookup_ids: EntityIds
    policy_version: str | None
    intent_hints: tuple[str, ...]
    routes: tuple[str, ...]


def analyze_intake(case: Mapping[str, Any]) -> CaseIntake:
    """Identify claims, lookup identifiers and routing hints from the input only.

    The customer message and claim topics are statements to verify, never facts.
    """
    case_id = str(case["case_id"])
    request = case.get("customer_request") or {}
    message = str(request.get("message") or "")
    claims: list[Claim] = []
    topics: dict[str, str] = {}
    for index, raw in enumerate(request.get("claims") or [], start=1):
        if isinstance(raw, Mapping):
            claim_id = str(raw.get("claim_id") or f"claim-{index:03d}")
            topic = str(raw.get("topic") or raw.get("text") or "")
        else:
            claim_id, topic = f"claim-{index:03d}", str(raw)
        claims.append(Claim(claim_id=claim_id, text=topic))
        topics[claim_id] = topic

    order_ids = []
    for key in ("claimed_order_id", "order_id"):
        if request.get(key):
            order_ids.append(str(request[key]))
        if case.get(key):
            order_ids.append(str(case[key]))
    lookup_ids = EntityIds(order_ids=tuple(order_ids))

    hints = [topic for topic in topics.values() if topic in TOPIC_ROUTES]
    if not hints:
        lowered = message.lower()
        hints = [code for code, pattern in TEXT_HINTS if pattern.search(lowered)]
    hints = sorted(set(hints))

    # Claims are unverified, so routing never narrows on them: every case checks all
    # three domains (the label comes from evidence). Hints only travel as task focus.
    routes: set[str] = {ORDER_AGENT, PAYMENT_AGENT, SHIPMENT_AGENT}
    ordered_routes = tuple(sorted(routes, key=PRIORITY.__getitem__))
    policy_version = case.get("policy_version")
    return CaseIntake(
        case_id=case_id,
        claims=tuple(claims),
        claim_topics=topics,
        lookup_ids=lookup_ids,
        policy_version=str(policy_version) if policy_version else None,
        intent_hints=tuple(hints),
        routes=ordered_routes,
    )


def to_json(value: Any) -> Any:
    """Convert internal values to JSON; Decimal must survive a float round-trip."""
    if isinstance(value, Decimal):
        converted = float(value)
        if Decimal(str(converted)) != value:
            raise ValueError(f"AMOUNT_NOT_JSON_SAFE: {value}")
        return converted
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite number in output")
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return to_json(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_json(item) for item in value]
    return value


@dataclass
class _Pending:
    priority: int
    sequence: int
    target: str
    payload: DomainTaskPayload
    reason: str


@dataclass
class _CaseState:
    intake: CaseIntake
    findings: dict[str, Finding] = field(default_factory=dict)
    entities: EntityIds = field(default_factory=EntityIds)
    evidence: list[str] = field(default_factory=list)
    conflicts: list[DataConflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    task_counter: int = 0
    queue_counter: int = 0
    task_keys: set[tuple[Any, ...]] = field(default_factory=set)
    domain_tasks: dict[int, int] = field(default_factory=lambda: {0: 0, 1: 0})

    def add_gap(self, gap: str) -> None:
        if gap not in self.gaps:
            self.gaps.append(gap)

    def add_evidence(self, refs: tuple[str, ...]) -> None:
        for ref in refs:
            if ref not in self.evidence:
                self.evidence.append(ref)

    def known_ids(self) -> EntityIds:
        known = self.intake.lookup_ids.union(self.entities)
        for finding in self.findings.values():
            known = known.union(finding.entity_ids)
        return known


class Coordinator:
    def __init__(
        self,
        *,
        agents: Mapping[str, Agent],
        contracts: Contracts,
        local_run_id: str,
        context_factory: Callable[[str, CaseTrace], dict[str, Any]] | None = None,
        task_deadline: float = TASK_DEADLINE_SECONDS,
    ) -> None:
        missing = [
            actor for actor in (*DOMAIN_AGENTS, POLICY_AGENT, VERIFIER) if actor not in agents
        ]
        if missing:
            raise ValueError(f"coordinator is missing agents: {missing}")
        self._agents = agents
        self._contracts = contracts
        self._run_id = local_run_id
        self._context_factory = context_factory
        self._task_deadline = task_deadline

    # ------------------------------------------------------------------ lifecycle
    async def solve(self, case: Mapping[str, Any], writer: Any) -> dict[str, Any]:
        intake = analyze_intake(case)
        trace = CaseTrace(writer, intake.case_id)
        runtime = self._context_factory(intake.case_id, trace) if self._context_factory else {}
        state = _CaseState(intake=intake)
        if intake.lookup_ids.is_empty():
            state.add_gap("NO_ORDER_IDENTIFIER")

        queue = [
            self._pending(
                state,
                target,
                self._domain_payload(state, intake.lookup_ids, ("INTAKE", *intake.intent_hints)),
                "INTAKE",
            )
            for target in intake.routes
        ]
        await self._investigate(state, trace, runtime, queue, attempt=0)
        if not state.evidence:
            # Transport/tool failures are not negative evidence; never finalize a case
            # (or let a batch be packaged) when no evidence could be collected at all.
            raise CaseFailedError(intake.case_id, "NO_EVIDENCE_COLLECTED", ",".join(state.gaps))

        revision = 0
        decision, candidate = await self._policy_and_candidate(state, trace, runtime, attempt=0)
        revision += 1
        verification = await self._verify(state, trace, runtime, decision, candidate, revision, 0)
        if verification.accepted:
            return candidate

        if not verification.remediation_requests:
            raise CaseFailedError(
                intake.case_id, "VERIFICATION_REJECTED", ",".join(verification.reason_codes)
            )

        queue = self._remediation_queue(state, verification)
        await self._investigate(state, trace, runtime, queue, attempt=1)
        decision, candidate = await self._policy_and_candidate(state, trace, runtime, attempt=1)
        revision += 1
        verification = await self._verify(state, trace, runtime, decision, candidate, revision, 1)
        if verification.accepted:
            return candidate
        raise CaseFailedError(
            intake.case_id, "REMEDIATION_FAILED", ",".join(verification.reason_codes)
        )

    # ------------------------------------------------------------ investigation
    def _domain_payload(
        self,
        state: _CaseState,
        entity_ids: EntityIds,
        focus: tuple[str, ...],
        claim_ids: tuple[str, ...] = (),
    ) -> DomainTaskPayload:
        claims = state.intake.claims
        if claim_ids:
            claims = tuple(claim for claim in claims if claim.claim_id in claim_ids) or claims
        return DomainTaskPayload(
            entity_ids=entity_ids,
            claims=claims,
            findings=tuple(state.findings.values()),
            evidence_context=tuple(state.evidence),
            focus=tuple(dict.fromkeys(focus)),
        )

    def _pending(
        self, state: _CaseState, target: str, payload: DomainTaskPayload, reason: str
    ) -> _Pending:
        state.queue_counter += 1
        return _Pending(PRIORITY[target], state.queue_counter, target, payload, reason)

    async def _investigate(
        self,
        state: _CaseState,
        trace: CaseTrace,
        runtime: dict[str, Any],
        queue: list[_Pending],
        attempt: int,
    ) -> None:
        budget = INITIAL_DOMAIN_BUDGET if attempt == 0 else REMEDIATION_DOMAIN_BUDGET
        while queue:
            queue.sort(key=lambda item: (item.priority, item.sequence))
            item = queue.pop(0)
            payload = item.payload
            # Refresh context so a handoff task sees findings gathered since it was queued.
            payload = DomainTaskPayload(
                entity_ids=payload.entity_ids,
                claims=payload.claims,
                findings=tuple(state.findings.values()),
                evidence_context=tuple(state.evidence),
                focus=payload.focus,
            )
            key = (
                item.target,
                TASK_TYPE_BY_AGENT[item.target],
                payload.entity_ids,
                payload.focus,
                attempt,
            )
            if key in state.task_keys:
                continue
            if state.domain_tasks[attempt] >= budget:
                state.add_gap(f"{item.target}:BUDGET_EXHAUSTED")
                continue
            state.task_keys.add(key)
            state.domain_tasks[attempt] += 1
            result = await self._dispatch(state, trace, runtime, item.target, payload, attempt)
            if result is None:
                continue
            self._absorb_domain(state, result)
            if attempt == 0:
                queue.extend(self._handoff_tasks(state, trace, result))
            elif isinstance(result.payload, DomainResult) and result.payload.handoff_requests:
                state.add_gap(f"{result.actor}:HANDOFF_NOT_ALLOWED_IN_REMEDIATION")

    def _absorb_domain(self, state: _CaseState, result: AgentResult) -> None:
        if result.status == "failed":
            state.add_gap(f"{result.actor}:{result.error_code or 'FAILED'}")
            return
        payload = result.payload
        if not isinstance(payload, DomainResult):
            return
        if result.status == "insufficient_evidence":
            state.add_gap(f"{result.actor}:INSUFFICIENT_EVIDENCE")
        for finding in payload.findings:
            state.findings[finding.finding_id] = finding
        state.entities = state.entities.union(payload.affected_entities)
        state.add_evidence(payload.evidence_refs)
        for conflict in payload.conflicts:
            if conflict not in state.conflicts:
                state.conflicts.append(conflict)
        for warning in payload.warnings:
            if warning not in state.warnings:
                state.warnings.append(warning)

    def _handoff_tasks(
        self, state: _CaseState, trace: CaseTrace, result: AgentResult
    ) -> list[_Pending]:
        payload = result.payload
        if not isinstance(payload, DomainResult):
            return []
        tasks = []
        known = state.known_ids()
        for request in payload.handoff_requests:
            valid_target = (
                request.target in DOMAIN_AGENTS
                and TASK_TYPE_BY_AGENT[request.target] == request.task_type
            )
            unknown_ids = [
                value
                for key in (
                    "order_ids",
                    "item_ids",
                    "seller_ids",
                    "payment_references",
                    "shipment_ids",
                )
                for value in getattr(request.entity_ids, key)
                if value not in getattr(known, key)
            ]
            if not valid_target or unknown_ids:
                state.add_gap(f"{result.actor}:HANDOFF_REJECTED")
                continue
            trace.emit(
                event_type="handoff",
                actor=result.actor,
                target=request.target,
                decision_code=request.reason_code[:80],
                evidence_refs=list(request.evidence_refs[:20]) or None,
                attributes={"task_id": result.task_id, "via": COORDINATOR},
            )
            entity_ids = (
                request.entity_ids if not request.entity_ids.is_empty() else state.known_ids()
            )
            tasks.append(
                self._pending(
                    state,
                    request.target,
                    self._domain_payload(
                        state, entity_ids, (request.reason_code,), request.claim_ids
                    ),
                    request.reason_code,
                )
            )
        return tasks

    def _remediation_queue(
        self, state: _CaseState, verification: VerificationResult
    ) -> list[_Pending]:
        focus_by_agent: dict[str, list[str]] = {}
        for request in verification.remediation_requests:
            targets = {request.target} if request.target in DOMAIN_AGENTS else set()
            targets.update(
                DOMAIN_TO_AGENT[d] for d in request.required_domains if d in DOMAIN_TO_AGENT
            )
            for target in targets:
                focus_by_agent.setdefault(target, []).append(request.reason_code)
        entity_ids = state.known_ids()
        return [
            self._pending(
                state,
                target,
                self._domain_payload(state, entity_ids, ("REMEDIATION", *sorted(set(codes)))),
                "REMEDIATION",
            )
            for target, codes in sorted(focus_by_agent.items(), key=lambda item: PRIORITY[item[0]])
        ]

    # ----------------------------------------------------------- policy / verify
    async def _policy_and_candidate(
        self, state: _CaseState, trace: CaseTrace, runtime: dict[str, Any], attempt: int
    ) -> tuple[PolicyDecision, dict[str, Any]]:
        payload = PolicyTaskPayload(
            claims=state.intake.claims,
            findings=tuple(state.findings.values()),
            affected_entities=state.entities,
            evidence_context=tuple(state.evidence),
            conflicts=tuple(state.conflicts),
            coverage_gaps=tuple(state.gaps),
            policy_version=state.intake.policy_version,
            intent_hints=state.intake.intent_hints,
        )
        result = await self._dispatch(state, trace, runtime, POLICY_AGENT, payload, attempt)
        if (
            result is None
            or result.status == "failed"
            or not isinstance(result.payload, PolicyDecision)
        ):
            code = result.error_code if result else "POLICY_FAILED"
            raise CaseFailedError(state.intake.case_id, "POLICY_FAILED", code or "")
        return result.payload, self.build_candidate(state, result.payload)

    def build_candidate(self, state: _CaseState, decision: PolicyDecision) -> dict[str, Any]:
        candidate: dict[str, Any] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": state.intake.case_id,
            "assessment": to_json(decision.assessment),
            "affected_entities": state.entities.to_public(),
            "root_cause_analysis": to_json(decision.root_cause_analysis),
            "evidence_refs": sorted(dict.fromkeys(decision.evidence_refs)),
            "data_conflicts": to_json(list(decision.data_conflicts)),
            "financial_resolution": to_json(decision.financial_resolution),
            "resolution_actions": list(dict.fromkeys(decision.resolution_actions)),
        }
        if decision.claim_assessments is not None:
            candidate["claim_assessments"] = to_json(list(decision.claim_assessments))
        self._contracts.validate_output(candidate, f"candidate {state.intake.case_id}")
        return candidate

    async def _verify(
        self,
        state: _CaseState,
        trace: CaseTrace,
        runtime: dict[str, Any],
        decision: PolicyDecision,
        candidate: dict[str, Any],
        revision: int,
        attempt: int,
    ) -> VerificationResult:
        payload = VerificationTaskPayload(
            candidate_output=copy.deepcopy(candidate),
            candidate_revision=revision,
            policy_decision=decision,
            findings=tuple(state.findings.values()),
            coverage_gaps=tuple(state.gaps),
            trace_snapshot=trace.snapshot(),
        )
        result = await self._dispatch(state, trace, runtime, VERIFIER, payload, attempt)
        if (
            result is None
            or result.status == "failed"
            or not isinstance(result.payload, VerificationResult)
        ):
            raise CaseFailedError(
                state.intake.case_id, "VERIFIER_FAILED", result.error_code if result else ""
            )
        return result.payload

    # --------------------------------------------------------------- dispatching
    async def _dispatch(
        self,
        state: _CaseState,
        trace: CaseTrace,
        runtime: dict[str, Any],
        target: str,
        payload: Any,
        attempt: int,
    ) -> AgentResult | None:
        state.task_counter += 1
        task = AgentTask(
            local_run_id=self._run_id,
            case_id=state.intake.case_id,
            task_id=f"{state.intake.case_id}-T{state.task_counter:02d}",
            sender=COORDINATOR,
            target=target,
            task_type=TASK_TYPE_BY_AGENT[target],  # type: ignore[arg-type]
            attempt=attempt,  # type: ignore[arg-type]
            payload=payload,
        )
        trace.emit(
            event_type="task_assigned",
            actor=COORDINATOR,
            target=target,
            attributes={"task_id": task.task_id, "task_type": task.task_type, "attempt": attempt},
        )
        context = AgentContext(
            local_run_id=self._run_id,
            case_id=task.case_id,
            trace=trace,
            deadline=time.monotonic() + self._task_deadline,
            **runtime,
        )
        result = await self._run_agent(task, context)
        reasons = self.check_result(task, result, context)
        if reasons and result.status != "failed":
            trace.emit(
                event_type="handoff",
                actor=target,
                target=COORDINATOR,
                decision_code="INVALID_SPECIALIST_RESULT",
                attributes={
                    "task_id": task.task_id,
                    "attempt": attempt,
                    "reasons": ",".join(reasons)[:200],
                },
            )
            context.repair_reason_codes = tuple(reasons)
            result = await self._run_agent(task, context)
            reasons = self.check_result(task, result, context)
        if reasons:
            result = self._failed(task, "INVALID_SPECIALIST_RESULT")
        trace.emit(
            event_type="handoff",
            actor=target,
            target=COORDINATOR,
            decision_code=self._decision_code(result),
            evidence_refs=self._result_refs(result)[:20] or None,
            attributes={"task_id": task.task_id, "attempt": attempt, "status": result.status},
        )
        return result

    async def _run_agent(self, task: AgentTask, context: AgentContext) -> AgentResult:
        agent = self._agents[task.target]
        try:
            return await asyncio.wait_for(
                agent.handle(task, context), timeout=max(0.1, context.remaining())
            )
        except TimeoutError:
            return self._failed(task, "TASK_DEADLINE_EXCEEDED")
        except GatewayFatalError:
            raise
        except Exception as exc:  # noqa: BLE001 - agent bug must not become a verdict
            return self._failed(task, f"AGENT_ERROR_{type(exc).__name__}"[:80])

    def _failed(self, task: AgentTask, code: str) -> AgentResult:
        return AgentResult(
            task.local_run_id, task.case_id, task.task_id, task.target, "failed", None, code
        )

    @staticmethod
    def _decision_code(result: AgentResult) -> str:
        if result.status == "failed":
            return result.error_code or "FAILED"
        if isinstance(result.payload, VerificationResult):
            return "VERIFICATION_ACCEPTED" if result.payload.accepted else "VERIFICATION_REJECTED"
        if isinstance(result.payload, PolicyDecision):
            return "POLICY_DECISION_RETURNED"
        if result.status == "needs_handoff" and isinstance(result.payload, DomainResult):
            return result.payload.handoff_requests[0].reason_code[:80]
        return "RESULT_" + result.status.upper()

    @staticmethod
    def _result_refs(result: AgentResult) -> list[str]:
        payload = result.payload
        if isinstance(payload, DomainResult | PolicyDecision):
            return list(dict.fromkeys(payload.evidence_refs))
        return []

    def check_result(self, task: AgentTask, result: Any, context: AgentContext) -> list[str]:
        """Validate an AgentResult against its task (ARCHITECTURE.md section 3)."""
        if not isinstance(result, AgentResult):
            return ["RESULT_TYPE_INVALID"]
        reasons = []
        if result.case_id != task.case_id:
            reasons.append("CASE_ID_MISMATCH")
        if result.local_run_id != task.local_run_id:
            reasons.append("RUN_ID_MISMATCH")
        if result.task_id != task.task_id:
            reasons.append("TASK_ID_MISMATCH")
        if result.actor != task.target:
            reasons.append("ACTOR_MISMATCH")
        if result.status not in RESULT_STATUSES:
            reasons.append("STATUS_INVALID")
        if result.status == "failed":
            if not result.error_code:
                reasons.append("FAILED_WITHOUT_ERROR_CODE")
            return reasons
        expected = {
            "apply_policy": PolicyDecision,
            "verify": VerificationResult,
        }.get(task.task_type, DomainResult)
        payload = result.payload
        if not isinstance(payload, expected):
            return [*reasons, "PAYLOAD_TYPE_MISMATCH"]
        ledger = context.ledger

        def ref_problems(refs: tuple[str, ...]) -> list[str]:
            problems = []
            for ref in refs:
                if not EVIDENCE_REF.fullmatch(ref):
                    problems.append("INVALID_EVIDENCE_REFS")
                elif ledger is not None and ref not in ledger:
                    problems.append("UNKNOWN_EVIDENCE_REF")
            return problems

        if isinstance(payload, DomainResult):
            reasons += ref_problems(payload.evidence_refs)
            result_refs = set(payload.evidence_refs)
            for finding in payload.findings:
                if not finding.evidence_refs or not set(finding.evidence_refs) <= result_refs:
                    reasons.append("FINDING_WITHOUT_EVIDENCE")
            if result.status == "needs_handoff" and not payload.handoff_requests:
                reasons.append("HANDOFF_WITHOUT_REQUEST")
        elif isinstance(payload, PolicyDecision):
            reasons += ref_problems(payload.evidence_refs)
            try:
                to_json(payload.financial_resolution)
            except ValueError:
                reasons.append("AMOUNT_NOT_JSON_SAFE")
        elif isinstance(payload, VerificationResult):
            expected_revision = task.payload.candidate_revision  # type: ignore[union-attr]
            if payload.candidate_revision != expected_revision:
                reasons.append("CANDIDATE_REVISION_MISMATCH")
            if payload.accepted and (payload.reason_codes or payload.remediation_requests):
                reasons.append("ACCEPTED_WITH_REASONS")
            if not payload.accepted and not payload.reason_codes:
                reasons.append("REJECTED_WITHOUT_REASON")
        return sorted(set(reasons))

    def candidate_is_valid(self, candidate: dict[str, Any]) -> bool:
        try:
            self._contracts.validate_output(candidate, "candidate")
        except ContractError:
            return False
        return True
