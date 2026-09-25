from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .contracts import ContractError, Contracts
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PRIMARY_ISSUES = frozenset(
    {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
        "insufficient_evidence",
    }
)

TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
}

AGENT_TOOL_PERMISSIONS = {
    "order-agent": frozenset({"get_order", "get_order_items", "get_sellers"}),
    "payment-agent": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "policy-agent": frozenset({"get_policy"}),
}

CORE_TOOLS = frozenset(
    {
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_sellers",
        "get_policy",
    }
)

EVIDENCE_ORDER = (
    "get_order",
    "get_order_items",
    "get_sellers",
    "get_order_payments",
    "get_payment_timeline",
    "get_refund_timeline",
    "get_shipment_summary",
    "get_policy",
)

PRIMARY_EVIDENCE = {
    "canceled_order_paid": (
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    ),
    "unavailable_order_paid": (
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_sellers",
        "get_policy",
    ),
    "late_delivery_seller": (
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_sellers",
        "get_policy",
    ),
    "late_delivery_logistics": (
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_sellers",
        "get_policy",
    ),
    "valid_split_payment": (
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    ),
    "payment_mismatch": (
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    ),
    "duplicate_charge": (
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    ),
    "refund_pending": (
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ),
    "refund_failed": (
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ),
    "unsupported_claim": (
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    ),
    "insufficient_evidence": EVIDENCE_ORDER,
}

PARTIAL_REFUND_ISSUES = frozenset(
    {"late_delivery_seller", "late_delivery_logistics", "payment_mismatch", "duplicate_charge"}
)

ISSUE_RESPONSIBILITY = {
    "canceled_order_paid": frozenset({"platform"}),
    "unavailable_order_paid": frozenset({"seller"}),
    "late_delivery_seller": frozenset({"seller"}),
    "late_delivery_logistics": frozenset({"logistics_provider"}),
    "valid_split_payment": frozenset({"customer"}),
    "payment_mismatch": frozenset({"payment_provider"}),
    "duplicate_charge": frozenset({"payment_provider"}),
    "refund_pending": frozenset({"payment_provider"}),
    "refund_failed": frozenset({"payment_provider"}),
    "unsupported_claim": frozenset({"customer"}),
    "insufficient_evidence": frozenset({"unknown"}),
}

ISSUE_ACTIONS = {
    "canceled_order_paid": "issue_refund",
    "unavailable_order_paid": "issue_refund",
    "late_delivery_seller": "refund_freight",
    "late_delivery_logistics": "refund_freight",
    "valid_split_payment": "document_no_action",
    "payment_mismatch": "reconcile_payment",
    "duplicate_charge": "refund_duplicate_charge",
    "refund_pending": "monitor_refund",
    "refund_failed": "retry_refund",
    "unsupported_claim": "document_no_action",
    "insufficient_evidence": "investigate_missing_evidence",
}

NO_ACTION_ISSUES = frozenset({"valid_split_payment", "unsupported_claim"})
NEEDS_INVESTIGATION_ISSUES = frozenset({"refund_pending", "insufficient_evidence"})
SCORING_POLICY_VERSION = "day09-scoring-v2"
TRACE_LIFECYCLE = (
    "case_received",
    "task_assigned",
    "tool_result_consumed",
    "handoff",
    "policy_decided",
    "verification_completed",
    "case_finalized",
)
SCORING_HARD_GATES = frozenset(
    {
        "case_id_mismatch",
        "unscorable_schema",
        "missing_required_evidence",
        "invalid_evidence_refs",
        "unknown_evidence_ref",
        "cross_scope_evidence_ref",
    }
)


@dataclass(frozen=True)
class EvidenceArtifact:
    tool_name: str
    evidence_ref: str
    domain: str
    data: Any
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Handoff:
    case_id: str
    sender: str
    receiver: str
    task: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ResponsibleParty:
    party_type: str
    party_id: str | None


@dataclass(frozen=True)
class PolicyDecision:
    primary_issue: str
    case_status: str
    action: str
    refund_brl: Decimal
    responsible_parties: tuple[ResponsibleParty, ...]
    confidence: float


class EvidenceCollector:
    """Collect scoped evidence while enforcing role and tool boundaries."""

    def __init__(
        self,
        case_id: str,
        order_id: str,
        policy_version: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> None:
        self.case_id = case_id
        self.order_id = order_id
        self.policy_version = policy_version
        self.gateway = gateway
        self.trace = trace
        self._artifacts: dict[str, EvidenceArtifact] = {}
        self._evidence_refs: set[str] = set()

    async def collect(self, actor: str, tool_name: str, **arguments: str) -> EvidenceArtifact:
        allowed_tools = AGENT_TOOL_PERMISSIONS.get(actor)
        if allowed_tools is None or tool_name not in allowed_tools:
            raise PermissionError(f"{actor} cannot call {tool_name}")
        if tool_name in self._artifacts:
            return self._artifacts[tool_name]

        response = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
        expected_domain = TOOL_DOMAINS[tool_name]
        if response.get("domain") != expected_domain:
            raise ContractError(
                f"{tool_name}: expected domain {expected_domain!r}, got {response.get('domain')!r}"
            )
        if tool_name == "get_policy":
            data = _object(response.get("data"), f"{tool_name}.data")
            if data.get("policy_version") != self.policy_version:
                raise ContractError(f"{tool_name}: policy_version does not match the case")
        else:
            if tool_name != "get_sellers":
                _require_order_scope(response.get("data"), self.order_id, tool_name)
            _assert_order_scope(response.get("data"), self.order_id, tool_name)

        evidence_ref = response["evidence_ref"]
        if evidence_ref in self._evidence_refs:
            raise ContractError(f"{tool_name}: duplicate evidence_ref in one case")
        self._evidence_refs.add(evidence_ref)
        artifact = EvidenceArtifact(
            tool_name=tool_name,
            evidence_ref=evidence_ref,
            domain=expected_domain,
            data=response["data"],
            warnings=tuple(response.get("warnings", [])),
        )
        self._artifacts[tool_name] = artifact
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
            attributes={"warning_count": len(response.get("warnings", []))},
        )
        return artifact

    def artifact(self, tool_name: str) -> EvidenceArtifact:
        try:
            return self._artifacts[tool_name]
        except KeyError as exc:
            raise ContractError(f"required evidence is missing: {tool_name}") from exc

    def has_artifact(self, tool_name: str) -> bool:
        return tool_name in self._artifacts

    def artifacts(self) -> tuple[EvidenceArtifact, ...]:
        return tuple(self._artifacts.values())

    def ordered_refs(self, tool_names: Iterable[str] = EVIDENCE_ORDER) -> list[str]:
        return [
            self._artifacts[tool_name].evidence_ref
            for tool_name in tool_names
            if tool_name in self._artifacts
        ]


class OrderItemAgent:
    actor = "order-agent"

    async def investigate(self, collector: EvidenceCollector, order_id: str) -> Handoff:
        await collector.collect(self.actor, "get_order", order_id=order_id)
        await collector.collect(self.actor, "get_order_items", order_id=order_id)
        await collector.collect(self.actor, "get_sellers", order_id=order_id)
        return _handoff(collector, self.actor, "policy-agent", "RESOLVE_ORDER_ITEMS")


class PaymentAgent:
    actor = "payment-agent"

    async def investigate(
        self, collector: EvidenceCollector, order_id: str, *, include_refunds: bool
    ) -> Handoff:
        await collector.collect(self.actor, "get_order_payments", order_id=order_id)
        await collector.collect(self.actor, "get_payment_timeline", order_id=order_id)
        if include_refunds:
            await collector.collect(self.actor, "get_refund_timeline", order_id=order_id)
        return _handoff(collector, self.actor, "policy-agent", "RECONCILE_PAYMENTS")


class ShipmentAgent:
    actor = "shipment-agent"

    async def investigate(self, collector: EvidenceCollector, order_id: str) -> Handoff:
        await collector.collect(self.actor, "get_shipment_summary", order_id=order_id)
        return _handoff(collector, self.actor, "policy-agent", "ASSESS_SHIPMENT")


def _validated_scoring_policy(contracts: Contracts) -> dict[str, Any]:
    policy = contracts.load_scoring_policy()
    if policy.get("policy_version") != SCORING_POLICY_VERSION:
        raise ContractError("scoring policy version is not supported")
    variants = policy.get("variant_weights")
    if not isinstance(variants, dict) or not isinstance(variants.get("l3a"), dict):
        raise ContractError("scoring policy does not define the l3a variant")
    required_events = policy.get("workflow_required_events")
    if not isinstance(required_events, list) or not set(required_events).issubset(TRACE_LIFECYCLE):
        raise ContractError("scoring policy has invalid workflow_required_events")
    if not SCORING_HARD_GATES.issubset(set(policy.get("hard_gates", []))):
        raise ContractError("scoring policy is missing required hard gates")
    return policy


class PolicyAgent:
    actor = "policy-agent"

    async def decide(
        self,
        collector: EvidenceCollector,
        hinted_issue: str | None,
    ) -> tuple[PolicyDecision, Handoff]:
        scoring_policy = _validated_scoring_policy(collector.trace.contracts)
        policy_artifact = await collector.collect(
            self.actor, "get_policy", policy_version=collector.policy_version
        )
        policy = _object(policy_artifact.data, "get_policy.data")
        if policy.get("currency") != "BRL":
            raise ContractError("get_policy: only BRL is supported by the L3A contract")
        rules_value = policy.get("rules")
        rules = rules_value if isinstance(rules_value, dict) else {}

        primary_issue = _classify_primary_issue(collector)
        if primary_issue not in rules:
            primary_issue = "insufficient_evidence"
        if primary_issue == "insufficient_evidence":
            case_status = "needs_investigation"
            action = "investigate_missing_evidence"
            refund = Decimal("0")
            responsible_parties = (ResponsibleParty("unknown", None),)
        else:
            rule = _object(rules.get(primary_issue), f"policy.rules.{primary_issue}")
            case_status = _string(rule.get("case_status"), "policy.case_status")
            action = _string(rule.get("recommended_action"), "policy.recommended_action")
            refund = _money(rule.get("refund_brl"), "policy.refund_brl")
            responsible_parties = _responsible_parties(rule.get("responsible_parties"))
            responsible_parties = _bind_seller_ids(
                responsible_parties, _seller_ids(collector)
            )

        confidence = _calibrated_confidence(collector, primary_issue, hinted_issue)
        decision = PolicyDecision(
            primary_issue=primary_issue,
            case_status=case_status,
            action=action,
            refund_brl=refund,
            responsible_parties=responsible_parties,
            confidence=confidence,
        )
        collector.trace.emit(
            case_id=collector.case_id,
            event_type="policy_decided",
            actor=self.actor,
            target=primary_issue,
            decision_code=primary_issue,
            evidence_refs=collector.ordered_refs(),
            attributes={
                "confidence": confidence,
                "scoring_policy_version": scoring_policy["policy_version"],
            },
        )
        return decision, _handoff(
            collector,
            self.actor,
            "verifier",
            "VERIFY_VALIDATED_OUTPUT",
            evidence_refs=collector.ordered_refs(),
        )


class VerifierAgent:
    actor = "verifier"

    def verify(
        self,
        case_id: str,
        claims: list[dict[str, str]],
        collector: EvidenceCollector,
        decision: PolicyDecision,
        hinted_issue: str | None,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        evidence_refs = collector.ordered_refs()
        output = _build_output(
            case_id=case_id,
            order_id=collector.order_id,
            claims=claims,
            collector=collector,
            decision=decision,
            evidence_refs=evidence_refs,
        )
        _verify_invariants(output, collector, decision)
        trace.contracts.validate_output(output, f"outputs/{case_id}.json")
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.actor,
            target="l3a-output-v2",
            decision_code="VERIFIED",
            evidence_refs=evidence_refs,
            attributes={"claim_count": len(output["claim_assessments"])},
        )
        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter, *,
    llm: JSONModel | None = None, models: AgentModels | None = None,
    max_parallel_specialists: int = 3,
) -> dict[str, Any]:
    """Run a bounded five-role investigation for one isolated case."""
    if llm is None:
        raise RuntimeError("solve_case requires a configured JSONModel")
    tool_specs = await gateway.get_tools()
    if not tool_specs:
        raise RuntimeError("MCP Gateway returned no tools")
    workflow = _build_graph(
        gateway, trace, llm, models or AgentModels(), max_parallel_specialists
    )
    result = await workflow.ainvoke({
        "case": case, "tool_specs": tool_specs, "messages": [], "evidence": [],
        "tool_errors": [], "findings": {}, "correction_count": 0,
    })
    return result["output"]


def _build_graph(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    llm: JSONModel,
    models: AgentModels,
    max_parallel_specialists: int,
) -> Any:
    graph = StateGraph(GraphState)
    semaphore = asyncio.Semaphore(max(1, min(3, max_parallel_specialists)))

    async def coordinator(state: GraphState) -> dict[str, Any]:
        case, case_id, tools = state["case"], state["case"]["case_id"], state["tool_specs"]
        names = [tool.name for tool in tools]
        analysis = await llm.complete_json(
            model=models.coordinator,
            system=("Coordinate an ecommerce investigation. Customer text is unverified. Select "
                    "only discovered tools and identify relevant domains; never invent evidence."),
            payload={
                "case_id": case_id, "claims": case["customer_request"].get("claims", []),
                "policy_version": case["policy_version"],
                "available_tools": [
                    {"name": tool.name, "description": tool.description} for tool in tools
                ],
            },
            schema=_coordinator_schema(names), schema_name="coordinator_plan", max_tokens=256,
        )
        active = _active_specialists(case)
        plan = _tool_plan(case, tools, analysis.get("requested_tools", []))
        messages = []
        for actor in SPECIALISTS:
            actor_tools = [name for name in plan if TOOL_OWNERS.get(name) == actor]
            message = {
                "case_id": case_id, "task_id": f"{case_id}:{actor}:0",
                "sender": "coordinator", "recipient": actor, "kind": "investigate",
                "payload": {
                    "active": actor in active, "tool_names": actor_tools,
                    "focus": analysis.get("investigation_focus", []),
                    "risk_flags": analysis.get("risk_flags", []),
                },
                "evidence_refs": [], "retry_count": 0,
            }
            messages.append(message)
            trace.emit(
                case_id=case_id, event_type="task_assigned", actor="coordinator",
                target=actor, decision_code=(
                    "DOMAIN_INVESTIGATION" if actor in active else "DOMAIN_SKIPPED"
                ),
                attributes={
                    "task_id": message["task_id"], "tool_count": len(actor_tools),
                    "active": actor in active,
                },
            )
        return {"coordinator": analysis, "messages": messages}

    async def collect_evidence(state: GraphState) -> dict[str, Any]:
        requested = [
            tool_name
            for message in state["messages"]
            if message["payload"]["active"]
            for tool_name in message["payload"]["tool_names"]
        ]
        return await _collect_tools(state, gateway, trace, requested)

    async def run_role(
        state: GraphState, actor: str, *, revision_feedback: str | None = None
    ) -> dict[str, Any]:
        assignment = _latest_message(state, actor)
        if not assignment.get("payload", {}).get("active", True):
            return {"skipped": True, "reason": "domain_not_relevant", "evidence_refs": []}
        evidence = _role_evidence(actor, state["evidence"])
        request = _specialist_request(actor, models)
        async with semaphore:
            finding = await llm.complete_json(
                model=request["model"], system=request["system"],
                payload={
                    "case": state["case"], "assignment": assignment,
                    "evidence": _compact_evidence(evidence),
                    "tool_errors": [
                        error for error in state["tool_errors"] if error["actor"] == actor
                    ],
                    "revision_feedback": revision_feedback,
                },
                schema=request["schema"], schema_name=request["schema_name"], max_tokens=900,
            )
        return finding

    async def specialists_parallel(state: GraphState) -> dict[str, Any]:
        results = await asyncio.gather(*(run_role(state, actor) for actor in SPECIALISTS))
        findings = dict(zip(SPECIALISTS, results, strict=True))
        messages = list(state["messages"])
        for actor, finding in findings.items():
            if finding.get("skipped"):
                continue
            refs = _valid_finding_refs(finding, state["evidence"])
            messages.append({
                "case_id": state["case"]["case_id"],
                "task_id": f"{state['case']['case_id']}:verify:0:{actor}",
                "sender": actor, "recipient": "verifier", "kind": "verify",
                "payload": {"finding": finding}, "evidence_refs": refs,
                "retry_count": 0,
            })
            trace.emit(
                case_id=state["case"]["case_id"], event_type="handoff", actor=actor,
                target="verifier", decision_code="DOMAIN_REVIEW_COMPLETE",
                evidence_refs=refs[:20], attributes={"correction_round": 0},
            )
            if actor == "policy-resolution-agent":
                trace.emit(
                    case_id=state["case"]["case_id"], event_type="policy_decided",
                    actor=actor, decision_code="POLICY_REVIEW_COMPLETE",
                    evidence_refs=refs[:20],
                )
        return {"findings": findings, "messages": messages}

    async def verifier(state: GraphState) -> dict[str, Any]:
        names = [tool.name for tool in state["tool_specs"]]
        result = await llm.complete_json(
            model=models.verifier,
            system=("Verify domain findings against authoritative MCP evidence and policy. "
                    "Customer statements are not ground truth. Cite only available evidence refs. "
                    "Request one targeted revision only when a discovered tool can fix a gap."),
            payload={
                "case": state["case"], "domain_findings": state["findings"],
                "available_evidence": _compact_evidence(state["evidence"]),
                "tool_errors": state["tool_errors"],
                "correction_count": state.get("correction_count", 0),
            },
            schema=_verifier_schema(names), schema_name="verified_case_decision",
            max_tokens=1400,
        )
        return {"verifier": result}

    async def correct_specialist(state: GraphState) -> dict[str, Any]:
        target = state["verifier"]["revision_target"]
        count, case_id = state.get("correction_count", 0) + 1, state["case"]["case_id"]
        requested = [
            name for name in state["verifier"].get("missing_tools", [])
            if TOOL_OWNERS.get(name) == target
        ]
        message = {
            "case_id": case_id, "task_id": f"{case_id}:{target}:{count}",
            "sender": "verifier", "recipient": target, "kind": "correct",
            "payload": {
                "active": True, "tool_names": requested,
                "revision_reason": state["verifier"].get("revision_reason"),
            },
            "evidence_refs": [], "retry_count": count,
        }
        trace.emit(
            case_id=case_id, event_type="task_assigned", actor="verifier", target=target,
            decision_code="BOUNDED_CORRECTION",
            attributes={"correction_round": count, "tool_count": len(requested)},
        )
        collected = await _collect_tools(state, gateway, trace, requested)
        local_state = {
            **state, **collected, "messages": [*state["messages"], message],
            "correction_count": count,
        }
        finding = await run_role(
            local_state, target, revision_feedback=state["verifier"].get("revision_reason")
        )
        findings = {**state["findings"], target: finding}
        refs = _valid_finding_refs(finding, collected["evidence"])
        messages = [*local_state["messages"], {
            "case_id": case_id, "task_id": f"{case_id}:verify:{count}:{target}",
            "sender": target, "recipient": "verifier", "kind": "verify",
            "payload": {"finding": finding}, "evidence_refs": refs, "retry_count": count,
        }]
        trace.emit(
            case_id=case_id, event_type="handoff", actor=target, target="verifier",
            decision_code="CORRECTION_COMPLETE", evidence_refs=refs[:20],
            attributes={"correction_round": count},
        )
        if target == "policy-resolution-agent":
            trace.emit(
                case_id=case_id, event_type="policy_decided", actor=target,
                decision_code="POLICY_CORRECTION_COMPLETE", evidence_refs=refs[:20],
            )
        return {
            **collected, "messages": messages, "findings": findings,
            "correction_count": count,
        }

    async def finalize(state: GraphState) -> dict[str, Any]:
        output = _build_output(state["case"], state["verifier"]["decision"], state["evidence"])
        trace.emit(
            case_id=state["case"]["case_id"], event_type="verification_completed",
            actor="verifier", target="coordinator",
            decision_code=output["assessment"]["primary_issue"].upper(),
            evidence_refs=output["evidence_refs"][:20],
            attributes={"correction_round": state.get("correction_count", 0)},
        )
        return {"output": output}

    def route_after_verifier(state: GraphState) -> str:
        result = state["verifier"]
        if (result.get("revision_required") and result.get("revision_target") in SPECIALISTS
                and state.get("correction_count", 0) < 1):
            return "correct_specialist"
        return "finalize"

    graph.add_node("coordinator", coordinator)
    graph.add_node("collect_evidence", collect_evidence)
    graph.add_node("specialists_parallel", specialists_parallel)
    graph.add_node("verifier", verifier)
    graph.add_node("correct_specialist", correct_specialist)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "coordinator")
    graph.add_edge("coordinator", "collect_evidence")
    graph.add_edge("collect_evidence", "specialists_parallel")
    graph.add_edge("specialists_parallel", "verifier")
    graph.add_conditional_edges(
        "verifier", route_after_verifier,
        {"correct_specialist": "correct_specialist", "finalize": "finalize"},
    )
    graph.add_edge("correct_specialist", "verifier")
    graph.add_edge("finalize", END)
    return graph.compile()


def _specialist_request(actor: str, models: AgentModels) -> dict[str, Any]:
    if actor == "order-payment-agent":
        return {
            "model": models.order_payment, "schema": ORDER_SCHEMA,
            "schema_name": "order_payment_finding",
            "system": ("Analyze only authoritative order, item, payment, and refund evidence. "
                       "Cite supplied evidence refs and report uncertainty."),
        }
    if actor == "shipment-seller-agent":
        return {
            "model": models.shipment_seller, "schema": SHIPMENT_SCHEMA,
            "schema_name": "shipment_seller_finding",
            "system": ("Analyze shipment timing and seller/logistics responsibility using only "
                       "supplied evidence refs."),
        }
    return {
        "model": models.policy_resolution, "schema": POLICY_SCHEMA,
        "schema_name": "policy_resolution_finding",
        "system": ("Apply the supplied policy to authoritative case facts. Do not treat customer "
                   "claims as facts and cite supplied evidence refs."),
    }


async def _collect_tools(
    state: GraphState, gateway: EvidenceGateway, trace: TraceWriter, requested: list[str],
) -> dict[str, Any]:
    """Resolve one complaint through a deterministic async A2A state-machine.

    The customer claim controls routing only. The primary issue is selected from
    scoped evidence by the policy agent and verified against the public L3A schema.
    """
    case_id = _non_empty_string(case.get("case_id"), "case.case_id")
    request = _object(case.get("customer_request"), "case.customer_request")
    order_id = _non_empty_string(request.get("claimed_order_id"), "claimed_order_id")
    policy_version = _non_empty_string(case.get("policy_version"), "case.policy_version")
    claims = _claims(request.get("claims"))
    hinted_issue = _issue_hint(claims)
    include_refunds = any(
        claim["topic"] in {"refund_pending", "refund_failed"} for claim in claims
    )

    required_tools = set(CORE_TOOLS)
    if include_refunds:
        required_tools.add("get_refund_timeline")
    discovered_tools = set(await gateway.list_tools())
    missing_tools = sorted(required_tools - discovered_tools)
    if missing_tools:
        raise RuntimeError(f"MCP tool discovery is missing required tools: {missing_tools}")

    collector = EvidenceCollector(case_id, order_id, policy_version, gateway, trace)
    assignments = (
        ("order-agent", "RESOLVE_ORDER_ITEMS"),
        ("payment-agent", "PAYMENT_RECONCILIATION"),
        ("shipment-agent", "SHIPMENT_ASSESSMENT"),
        ("policy-agent", "POLICY_DECISION"),
        ("verifier", "CONTRACT_VERIFICATION"),
    )
    for target, decision_code in assignments:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
            decision_code=decision_code,
        )

    order_handoff = await OrderItemAgent().investigate(collector, order_id)
    payment_handoff = await PaymentAgent().investigate(
        collector, order_id, include_refunds=include_refunds
    )
    shipment_handoff = await ShipmentAgent().investigate(collector, order_id)
    for handoff in (order_handoff, payment_handoff, shipment_handoff):
        _emit_handoff(trace, handoff)

    decision, policy_handoff = await PolicyAgent().decide(collector, hinted_issue)
    _emit_handoff(trace, policy_handoff)
    return VerifierAgent().verify(
        case_id,
        claims,
        collector,
        decision,
        hinted_issue,
        trace,
    )


def _classify_primary_issue(collector: EvidenceCollector) -> str:
    try:
        return _classify_primary_issue_from_evidence(collector)
    except ContractError:
        return "insufficient_evidence"


def _classify_primary_issue_from_evidence(collector: EvidenceCollector) -> str:
    order = _object(collector.artifact("get_order").data, "get_order.data")
    items = _rows(collector.artifact("get_order_items").data, "get_order_items.data")
    payments = _rows(collector.artifact("get_order_payments").data, "get_order_payments.data")
    payment_timeline = _object(
        collector.artifact("get_payment_timeline").data, "get_payment_timeline.data"
    )
    shipment = _object(
        collector.artifact("get_shipment_summary").data, "get_shipment_summary.data"
    )
    payment_events = _rows(payment_timeline.get("events"), "get_payment_timeline.events")
    if not items or not payments:
        return "insufficient_evidence"
    confirmed_capture_counts = Counter(
        amount
        for event in payment_events
        if event.get("event_type") == "captured"
        and event.get("status") == "confirmed"
        and (amount := _positive_decimal(event.get("amount_brl"))) is not None
    )
    confirmed_capture_amounts = set(confirmed_capture_counts)

    if collector.has_artifact("get_refund_timeline"):
        refund_data = _object(
            collector.artifact("get_refund_timeline").data, "get_refund_timeline.data"
        )
        refund_events = _rows(refund_data.get("events"), "get_refund_timeline.events")
        if refund_events:
            latest_refund = max(refund_events, key=_event_time_key)
            if latest_refund.get("status") == "pending":
                return "refund_pending"
            if latest_refund.get("status") == "failed":
                return "refund_failed"

    order_status = order.get("order_status")
    if order_status == "canceled" and confirmed_capture_amounts:
        return "canceled_order_paid"
    if order_status == "unavailable" and confirmed_capture_amounts:
        return "unavailable_order_paid"

    shipment_events = _rows(shipment.get("events"), "get_shipment_summary.events")
    delivered_customer_at = shipment.get("delivered_customer_at")
    late_actor = next(
        (
            event.get("actor")
            for event in shipment_events
            if event.get("event_type") == "delivered_late"
            and event.get("status") == "confirmed"
            and _same_instant(event.get("event_at"), delivered_customer_at)
        ),
        None,
    )
    if late_actor == "seller":
        return "late_delivery_seller"
    if late_actor == "logistics_provider":
        return "late_delivery_logistics"

    if any(
        event.get("event_type") == "reconciliation_mismatch"
        and _positive_decimal(event.get("amount_brl")) is not None
        for event in payment_events
    ):
        return "payment_mismatch"

    payment_groups: dict[tuple[str, Decimal], list[Mapping[str, Any]]] = defaultdict(list)
    split_types_by_amount: dict[Decimal, set[str]] = defaultdict(set)
    for payment in payments:
        payment_type = _non_empty_string(payment.get("payment_type"), "payment_type")
        amount = _positive_decimal(payment.get("payment_value"))
        if amount is None:
            return "insufficient_evidence"
        payment_groups[(payment_type, amount)].append(payment)
        if payment_type in {"credit_card", "voucher"}:
            split_types_by_amount[amount].add(payment_type)
    if any(
        len(group) > 1 and key[1] in confirmed_capture_amounts
        for key, group in payment_groups.items()
    ):
        return "duplicate_charge"

    split_payment_types = (
        set().union(*split_types_by_amount.values()) if split_types_by_amount else set()
    )
    split_is_fully_captured = all(
        confirmed_capture_counts[amount] >= len(payment_types)
        for amount, payment_types in split_types_by_amount.items()
    )
    if {"credit_card", "voucher"}.issubset(split_payment_types) and split_is_fully_captured:
        return "valid_split_payment"
    return "unsupported_claim"


def _build_output(
    *,
    case_id: str,
    order_id: str,
    claims: list[dict[str, str]],
    collector: EvidenceCollector,
    decision: PolicyDecision,
    evidence_refs: list[str],
) -> dict[str, Any]:
    if decision.primary_issue == "insufficient_evidence":
        order = _object_or_empty(collector.artifact("get_order").data)
        items = _rows_or_empty(collector.artifact("get_order_items").data)
        payments = _rows_or_empty(collector.artifact("get_order_payments").data)
        shipment = _object_or_empty(collector.artifact("get_shipment_summary").data)
        seller_ids = _seller_ids_or_empty(collector)
    else:
        order = _object(collector.artifact("get_order").data, "get_order.data")
        items = _rows(collector.artifact("get_order_items").data, "get_order_items.data")
        payments = _rows(
            collector.artifact("get_order_payments").data, "get_order_payments.data"
        )
        shipment = _object(
            collector.artifact("get_shipment_summary").data, "get_shipment_summary.data"
        )
        seller_ids = _seller_ids(collector)
    order_ids = _bounded_strings({_text(order.get("order_id")), order_id})
    item_ids = _bounded_strings(
        _text(item.get("order_item_id")) for item in items if item.get("order_item_id")
    )
    payment_references = _payment_references(payments)
    shipment_ids = _shipment_ids(shipment)

    refund = _rounded_money(decision.refund_brl)
    refund_entity = order_id
    if decision.primary_issue in {"payment_mismatch", "duplicate_charge"} and payment_references:
        refund_entity = payment_references[0]
    elif decision.primary_issue == "late_delivery_seller":
        policy_seller_ids = {
            party.party_id
            for party in decision.responsible_parties
            if party.party_type == "seller" and party.party_id is not None
        }
        if len(policy_seller_ids) == 1:
            refund_entity = next(iter(policy_seller_ids))
        elif len(seller_ids) == 1:
            refund_entity = seller_ids[0]
        else:
            refund_entity = None
    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": decision.action,
                "amount_brl": refund,
                "entity_id": refund_entity,
            }
        )
    responsible_parties = [
        {"party_type": party.party_type, "party_id": party.party_id}
        for party in decision.responsible_parties
    ]
    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": decision.primary_issue,
            "case_status": decision.case_status,
            "confidence": decision.confidence,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": _claim_assessments(
            claims, decision.primary_issue, collector, decision.confidence
        ),
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": decision.primary_issue.upper(), "rank": 1}
            ],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": _data_conflicts(items, payments),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [decision.action],
    }
    return output


def _claim_assessments(
    claims: list[dict[str, str]],
    primary_issue: str,
    collector: EvidenceCollector,
    assessment_confidence: float,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for claim in claims[:5]:
        topic = claim["topic"]
        if topic in PRIMARY_ISSUES:
            if primary_issue == "insufficient_evidence":
                verdict = "insufficient_evidence"
                confidence = 0.30
            else:
                verdict = "supported" if topic == primary_issue else "unsupported"
                confidence = 0.95 if verdict == "supported" else 0.80
            tools = PRIMARY_EVIDENCE[primary_issue]
        elif topic == "requested_full_refund":
            if primary_issue in {"refund_pending", "insufficient_evidence"}:
                verdict = "insufficient_evidence"
            elif _refund_is_zero(collector, primary_issue):
                verdict = "unsupported"
            elif primary_issue in PARTIAL_REFUND_ISSUES:
                verdict = "partially_supported"
            else:
                verdict = "supported"
            confidence = 0.93
            tools = _refund_claim_tools(primary_issue)
        else:
            verdict = "insufficient_evidence"
            confidence = 0.50
            tools = EVIDENCE_ORDER
        confidence = min(confidence, max(assessment_confidence + 0.02, 0.30))
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": collector.ordered_refs(tools),
            }
        )
    return result


def _refund_is_zero(collector: EvidenceCollector, primary_issue: str) -> bool:
    policy = _object(collector.artifact("get_policy").data, "get_policy.data")
    rules = _object(policy.get("rules"), "get_policy.data.rules")
    rule = _object(rules.get(primary_issue), f"policy.rules.{primary_issue}")
    return _money(rule.get("refund_brl"), "policy.refund_brl") == 0


def _refund_claim_tools(primary_issue: str) -> tuple[str, ...]:
    if primary_issue in {"refund_pending", "refund_failed"}:
        return (
            "get_order",
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        )
    if primary_issue in {"payment_mismatch", "duplicate_charge"}:
        return (
            "get_order",
            "get_order_payments",
            "get_payment_timeline",
            "get_policy",
        )
    if primary_issue in {"late_delivery_seller", "late_delivery_logistics"}:
        return (
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_policy",
        )
    return ("get_order", "get_order_payments", "get_payment_timeline", "get_policy")


def _data_conflicts(
    items: list[Mapping[str, Any]], payments: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    item_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in items:
        item_id = item.get("order_item_id")
        if item_id:
            item_groups[_text(item_id)].append(item)
    for group in item_groups.values():
        for field in ("seller_id", "price", "freight_value", "shipping_limit_date"):
            conflicts.extend(
                _conflict_records(f"order_items.{field}", group, "order_items")
            )

    payment_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for payment in payments:
        key = (
            _text(payment.get("payment_sequential")),
            _text(payment.get("payment_type")),
        )
        payment_groups[key].append(payment)
    for group in payment_groups.values():
        conflicts.extend(
            _conflict_records(
                "order_payments.payment_value", group, "order_payments"
            )
        )
    return conflicts[:5]


def _conflict_records(
    field: str,
    rows: list[Mapping[str, Any]],
    tool_name: str,
) -> list[dict[str, Any]]:
    field_name = field.rsplit(".", 1)[-1]
    if len({_text(row.get(field_name)) for row in rows}) < 2:
        return []
    return [
        {
            "field": field,
            "sources": [
                f"{tool_name}[{index}].{field_name}" for index in range(len(rows))
            ][:5],
            "selected_source": None,
            "resolution_code": "DUPLICATE_ID_WITH_DIFFERENT_VALUES",
        }
    ]


def _verify_invariants(
    output: dict[str, Any], collector: EvidenceCollector, decision: PolicyDecision
) -> None:
    case_id = collector.case_id
    if output.get("case_id") != case_id:
        raise ContractError(f"verifier: case_id mismatch for {case_id}")
    known_refs = set(collector.ordered_refs())
    if set(output["evidence_refs"]) != known_refs:
        raise ContractError("verifier: output evidence registry is incomplete")
    for claim in output["claim_assessments"]:
        if not set(claim["evidence_refs"]).issubset(known_refs):
            raise ContractError("verifier: claim references unknown evidence")

    issue = decision.primary_issue
    if output["assessment"]["primary_issue"] != issue:
        raise ContractError("verifier: policy decision was not preserved")
    if output["assessment"]["confidence"] != decision.confidence:
        raise ContractError("verifier: calibrated confidence was not preserved")
    expected_status = (
        "no_action"
        if issue in NO_ACTION_ISSUES
        else "needs_investigation"
        if issue in NEEDS_INVESTIGATION_ISSUES
        else "action_required"
    )
    if (
        decision.case_status != expected_status
        or output["assessment"]["case_status"] != expected_status
    ):
        raise ContractError("verifier: case status is inconsistent with the primary issue")
    if decision.action != ISSUE_ACTIONS[issue] or output["resolution_actions"] != [decision.action]:
        raise ContractError("verifier: resolution action is inconsistent with the policy")

    root_cause = output["root_cause_analysis"]
    if root_cause["ranked_causes"][0]["cause_code"] != issue.upper():
        raise ContractError("verifier: root cause is inconsistent with the primary issue")
    expected_party_types = ISSUE_RESPONSIBILITY[issue]
    actual_party_types = {
        party["party_type"] for party in root_cause["responsible_parties"]
    }
    if actual_party_types != set(expected_party_types):
        raise ContractError("verifier: responsible party is inconsistent with the primary issue")
    seller_ids = set(output["affected_entities"]["seller_ids"])
    for party in root_cause["responsible_parties"]:
        if (
            party["party_type"] == "seller"
            and party["party_id"] is not None
            and party["party_id"] not in seller_ids
        ):
            raise ContractError("verifier: responsible seller is outside the affected seller set")

    financial = output["financial_resolution"]
    recommended_refund = Decimal(str(financial["recommended_refund_brl"]))
    if recommended_refund != decision.refund_brl:
        raise ContractError("verifier: recommended refund was changed after policy")
    line_total = sum(
        (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]),
        Decimal("0"),
    )
    if line_total != recommended_refund:
        raise ContractError("verifier: refund lines do not equal recommended refund")
    if (
        issue in NO_ACTION_ISSUES | NEEDS_INVESTIGATION_ISSUES
        and (recommended_refund != 0 or financial["refund_lines"])
    ):
        raise ContractError("verifier: non-refund issue must not create a refund line")
    order_ids = set(output["affected_entities"]["order_ids"])
    payment_references = set(output["affected_entities"]["payment_references"])
    for line in financial["refund_lines"]:
        if line["reason_code"] != decision.action:
            raise ContractError("verifier: refund reason does not match the resolution action")
        entity_id = line["entity_id"]
        if (
            issue in {"payment_mismatch", "duplicate_charge"}
            and entity_id not in payment_references
        ):
            raise ContractError("verifier: payment refund is not linked to a payment reference")
        if issue == "late_delivery_seller" and entity_id not in seller_ids:
            raise ContractError("verifier: seller refund is not linked to the responsible seller")
        if (
            issue
            in {
                "canceled_order_paid",
                "unavailable_order_paid",
                "late_delivery_logistics",
                "refund_failed",
            }
            and entity_id not in order_ids
        ):
            raise ContractError("verifier: order refund is not linked to the order")


def _handoff(
    collector: EvidenceCollector,
    sender: str,
    receiver: str,
    task: str,
    *,
    evidence_refs: Iterable[str] | None = None,
) -> Handoff:
    refs = (
        tuple(evidence_refs)
        if evidence_refs is not None
        else tuple(
            collector.ordered_refs(
                tool_name
                for tool_name in EVIDENCE_ORDER
                if tool_name in AGENT_TOOL_PERMISSIONS[sender]
            )
        )
    )
    return Handoff(
        case_id=collector.case_id,
        sender=sender,
        receiver=receiver,
        task=task,
        evidence_refs=refs,
    )


def _emit_handoff(trace: TraceWriter, handoff: Handoff) -> None:
    trace.emit(
        case_id=handoff.case_id,
        event_type="handoff",
        actor=handoff.sender,
        target=handoff.receiver,
        decision_code=handoff.task,
        evidence_refs=list(handoff.evidence_refs) or None,
    )


def _calibrated_confidence(
    collector: EvidenceCollector, primary_issue: str, hinted_issue: str | None
) -> float:
    confidence = _assessment_confidence(primary_issue, hinted_issue)
    if primary_issue == "insufficient_evidence":
        return confidence
    try:
        items = _rows(collector.artifact("get_order_items").data, "get_order_items.data")
        payments = _rows(
            collector.artifact("get_order_payments").data, "get_order_payments.data"
        )
        conflict_count = len(_data_conflicts(items, payments))
    except ContractError:
        conflict_count = 1
    warning_count = sum(len(artifact.warnings) for artifact in collector.artifacts())
    confidence -= min(0.15, conflict_count * 0.03)
    confidence -= min(0.10, warning_count * 0.02)
    return round(max(0.0, min(1.0, confidence)), 4)


def _assessment_confidence(primary_issue: str, hinted_issue: str | None) -> float:
    if primary_issue == "insufficient_evidence":
        return 0.20
    if primary_issue == hinted_issue:
        return 0.97
    return 0.78


def _seller_ids(collector: EvidenceCollector) -> list[str]:
    ids: set[str] = set()
    items = _rows(collector.artifact("get_order_items").data, "get_order_items.data")
    for item in items:
        if item.get("seller_id"):
            ids.add(_text(item["seller_id"]))
    sellers = _rows(collector.artifact("get_sellers").data, "get_sellers.data")
    for seller in sellers:
        if seller.get("seller_id"):
            ids.add(_text(seller["seller_id"]))
    return _bounded_strings(ids)


def _seller_ids_or_empty(collector: EvidenceCollector) -> list[str]:
    try:
        return _seller_ids(collector)
    except ContractError:
        return []


def _payment_references(payments: list[Mapping[str, Any]]) -> list[str]:
    references: set[str] = set()
    for payment in payments:
        for key in ("payment_reference", "payment_id", "transaction_id", "payment_sequential"):
            value = payment.get(key)
            if value is not None and _text(value):
                references.add(_text(value))
    return sorted(
        references,
        key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
    )


def _shipment_ids(shipment: Mapping[str, Any]) -> list[str]:
    values: set[str] = set()
    for key in ("shipment_id", "shipment_tracking_id", "tracking_id"):
        value = shipment.get(key)
        if value:
            values.add(_text(value))
    return _bounded_strings(values)


def _bind_seller_ids(
    parties: tuple[ResponsibleParty, ...], seller_ids: list[str]
) -> tuple[ResponsibleParty, ...]:
    unique_seller_id = seller_ids[0] if len(seller_ids) == 1 else None
    known_seller_ids = set(seller_ids)
    resolved: list[ResponsibleParty] = []
    for party in parties:
        if party.party_type != "seller" or party.party_id in known_seller_ids:
            resolved.append(party)
        else:
            resolved.append(ResponsibleParty(party.party_type, unique_seller_id))
    return tuple(resolved)


def _responsible_parties(value: Any) -> tuple[ResponsibleParty, ...]:
    rows = _rows(value, "policy.responsible_parties")[:5]
    allowed_types = {
        "seller",
        "platform",
        "logistics_provider",
        "payment_provider",
        "customer",
        "unknown",
    }
    result: list[ResponsibleParty] = []
    for row in rows:
        party_type = _non_empty_string(row.get("party_type"), "party_type")
        if party_type not in allowed_types:
            raise ContractError(f"unsupported responsible party type: {party_type}")
        party_id = row.get("party_id")
        if party_id is not None:
            party_id = _text(party_id)
        result.append(ResponsibleParty(party_type, party_id))
    return tuple(result)


def _claims(value: Any) -> list[dict[str, str]]:
    rows = _rows(value, "customer_request.claims")
    result: list[dict[str, str]] = []
    for row in rows:
        result.append(
            {
                "claim_id": _non_empty_string(row.get("claim_id"), "claim_id"),
                "topic": _non_empty_string(row.get("topic"), "claim.topic"),
            }
        )
    return result


def _issue_hint(claims: list[dict[str, str]]) -> str | None:
    return next((claim["topic"] for claim in claims if claim["topic"] in PRIMARY_ISSUES), None)


def _require_order_scope(value: Any, order_id: str, label: str) -> None:
    rows = value if isinstance(value, list) else [value]
    for row in rows:
        if not isinstance(row, Mapping) or row.get("order_id") != order_id:
            raise ContractError(f"{label}: missing or cross-scope order_id")


def _assert_order_scope(value: Any, order_id: str, label: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key == "order_id" and nested != order_id:
                raise ContractError(f"{label}: cross-scope order_id")
            _assert_order_scope(nested, order_id, label)
    elif isinstance(value, list):
        for nested in value:
            _assert_order_scope(nested, order_id, label)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label}: expected an object")
    return value


def _object_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ContractError(f"{label}: expected an array of objects")
    return list(value)


def _rows_or_empty(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{label}: expected a non-empty string")
    return value


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _string(value: Any, label: str) -> str:
    result = _non_empty_string(value, label)
    if len(result) > 80:
        raise ContractError(f"{label}: exceeds 80 characters")
    return result


def _money(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractError(f"{label}: expected a number") from exc
    if not result.is_finite() or result < 0:
        raise ContractError(f"{label}: expected a finite non-negative number")
    return result.quantize(Decimal("0.01"))


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same_instant(left: Any, right: Any) -> bool:
    left_instant = _instant(left)
    right_instant = _instant(right)
    if left_instant is not None and right_instant is not None:
        return _normalized_instant(left_instant) == _normalized_instant(right_instant)
    return left == right


def _event_time_key(event: Mapping[str, Any]) -> tuple[datetime, str]:
    instant = _instant(event.get("event_at"))
    normalized = (
        _normalized_instant(instant)
        if instant is not None
        else datetime.min.replace(tzinfo=UTC)
    )
    return normalized, _text(event.get("event_at"))


def _normalized_instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _rounded_money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _bounded_strings(values: Iterable[str]) -> list[str]:
    result = sorted({value for value in values if value})
    if len(result) > 20:
        raise ContractError("an entity set exceeds the public limit of 20")
    if any(len(value) > 128 for value in result):
        raise ContractError("an entity identifier exceeds the public limit of 128")
    return result
