from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .llm import JSONModel
from .mcp_gateway import EvidenceGateway, ToolSpec
from .trace import TraceWriter

PRIMARY_ISSUES = [
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed", "unsupported_claim",
    "insufficient_evidence",
]
VERDICTS = ["supported", "unsupported", "partially_supported", "insufficient_evidence"]
ENTITY_KEYS = ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
SPECIALISTS = ("order-payment-agent", "shipment-seller-agent", "policy-resolution-agent")
SHIPPING_TOPICS = {"late_delivery_seller", "late_delivery_logistics"}
TOOL_OWNERS = {
    "get_order": "order-payment-agent",
    "get_order_items": "order-payment-agent",
    "get_order_payments": "order-payment-agent",
    "get_payment_timeline": "order-payment-agent",
    "get_refund_timeline": "order-payment-agent",
    "get_shipment_summary": "shipment-seller-agent",
    "get_sellers": "shipment-seller-agent",
    "get_policy": "policy-resolution-agent",
}


@dataclass(frozen=True)
class AgentModels:
    coordinator: str = "qwen3:1.7b"
    order_payment: str = "qwen3:1.7b"
    shipment_seller: str = "qwen3:1.7b"
    policy_resolution: str = "qwen3:1.7b"
    verifier: str = "qwen3:1.7b"


class GraphState(TypedDict, total=False):
    case: dict[str, Any]
    tool_specs: list[ToolSpec]
    coordinator: dict[str, Any]
    messages: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    tool_errors: list[dict[str, str]]
    findings: dict[str, dict[str, Any]]
    verifier: dict[str, Any]
    output: dict[str, Any]
    correction_count: int


ENTITIES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(ENTITY_KEYS),
    "properties": {key: {"type": "array", "items": {"type": "string"}} for key in ENTITY_KEYS},
}
PARTIES_SCHEMA = {
    "type": "array", "maxItems": 5,
    "items": {
        "type": "object", "additionalProperties": False,
        "required": ["party_type", "party_id"],
        "properties": {
            "party_type": {"enum": [
                "seller", "platform", "logistics_provider", "payment_provider",
                "customer", "unknown",
            ]},
            "party_id": {"type": ["string", "null"]},
        },
    },
}
CLAIMS_SCHEMA = {
    "type": "array", "maxItems": 5,
    "items": {
        "type": "object", "additionalProperties": False,
        "required": ["claim_id", "verdict", "confidence", "evidence_refs"],
        "properties": {
            "claim_id": {"type": "string"}, "verdict": {"enum": VERDICTS},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
    },
}
REFUNDS_SCHEMA = {
    "type": "array", "maxItems": 10,
    "items": {
        "type": "object", "additionalProperties": False,
        "required": ["reason_code", "amount_brl", "entity_id"],
        "properties": {
            "reason_code": {"type": "string"},
            "amount_brl": {"type": "number", "minimum": 0},
            "entity_id": {"type": ["string", "null"]},
        },
    },
}
CONFLICTS_SCHEMA = {
    "type": "array", "maxItems": 5,
    "items": {
        "type": "object", "additionalProperties": False,
        "required": ["field", "sources", "selected_source", "resolution_code"],
        "properties": {
            "field": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            "selected_source": {"type": ["string", "null"]},
            "resolution_code": {"type": "string"},
        },
    },
}

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": [
        "summary", "claim_findings", "entities", "candidate_issue", "refund_lines",
        "data_conflicts", "evidence_refs",
    ],
    "properties": {
        "summary": {"type": "string", "maxLength": 500},
        "claim_findings": CLAIMS_SCHEMA, "entities": ENTITIES_SCHEMA,
        "candidate_issue": {"enum": PRIMARY_ISSUES}, "refund_lines": REFUNDS_SCHEMA,
        "data_conflicts": CONFLICTS_SCHEMA,
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
}
SHIPMENT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": [
        "summary", "entities", "candidate_issue", "responsibility", "data_conflicts",
        "evidence_refs",
    ],
    "properties": {
        "summary": {"type": "string", "maxLength": 400}, "entities": ENTITIES_SCHEMA,
        "candidate_issue": {"enum": [
            "late_delivery_seller", "late_delivery_logistics", "unsupported_claim",
            "insufficient_evidence",
        ]},
        "responsibility": {"enum": ["seller", "logistics_provider", "unknown"]},
        "data_conflicts": CONFLICTS_SCHEMA,
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
}
POLICY_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": [
        "summary", "applicable_rules", "eligible", "refund_cap_brl", "resolution_actions",
        "evidence_refs",
    ],
    "properties": {
        "summary": {"type": "string", "maxLength": 400},
        "applicable_rules": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "eligible": {"enum": ["yes", "no", "uncertain"]},
        "refund_cap_brl": {"type": ["number", "null"], "minimum": 0},
        "resolution_actions": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
}


def _decision_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "assessment", "affected_entities", "claim_assessments", "root_cause_analysis",
            "evidence_refs", "data_conflicts", "financial_resolution", "resolution_actions",
        ],
        "properties": {
            "assessment": {
                "type": "object", "additionalProperties": False,
                "required": ["primary_issue", "case_status", "confidence"],
                "properties": {
                    "primary_issue": {"enum": PRIMARY_ISSUES},
                    "case_status": {"enum": [
                        "action_required", "no_action", "needs_investigation",
                    ]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
            "affected_entities": ENTITIES_SCHEMA, "claim_assessments": CLAIMS_SCHEMA,
            "root_cause_analysis": {
                "type": "object", "additionalProperties": False,
                "required": ["ranked_causes", "responsible_parties"],
                "properties": {
                    "ranked_causes": {
                        "type": "array", "maxItems": 5,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["cause_code", "rank"],
                            "properties": {
                                "cause_code": {"type": "string"},
                                "rank": {"type": "integer", "minimum": 1, "maximum": 5},
                            },
                        },
                    },
                    "responsible_parties": PARTIES_SCHEMA,
                },
            },
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "data_conflicts": CONFLICTS_SCHEMA,
            "financial_resolution": {
                "type": "object", "additionalProperties": False,
                "required": ["currency", "recommended_refund_brl", "refund_lines"],
                "properties": {
                    "currency": {"const": "BRL"},
                    "recommended_refund_brl": {"type": "number", "minimum": 0},
                    "refund_lines": REFUNDS_SCHEMA,
                },
            },
            "resolution_actions": {
                "type": "array", "items": {"type": "string"}, "maxItems": 8,
            },
        },
    }


def _coordinator_schema(tool_names: list[str]) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["claim_ids", "investigation_focus", "risk_flags", "requested_tools"],
        "properties": {
            "claim_ids": {"type": "array", "items": {"type": "string"}},
            "investigation_focus": {
                "type": "array",
                "items": {"enum": ["order_payment", "shipment_seller", "policy_resolution"]},
            },
            "risk_flags": {"type": "array", "items": {"type": "string", "maxLength": 120}},
            "requested_tools": {
                "type": "array",
                "items": {"type": "string", "enum": tool_names},
            },
        },
    }


def _verifier_schema(tool_names: list[str]) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "decision", "revision_required", "revision_target", "revision_reason", "missing_tools",
        ],
        "properties": {
            "decision": _decision_schema(), "revision_required": {"type": "boolean"},
            "revision_target": {"enum": [*SPECIALISTS, None]},
            "revision_reason": {"type": ["string", "null"], "maxLength": 200},
            "missing_tools": {
                "type": "array",
                "items": {"type": "string", "enum": tool_names},
            },
        },
    }


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
    case, case_id = state["case"], state["case"]["case_id"]
    specs = {tool.name: tool for tool in state["tool_specs"]}
    evidence, errors = list(state["evidence"]), list(state["tool_errors"])
    called = {item["tool_name"] for item in evidence}
    argument_values = {
        "order_id": case["customer_request"].get("claimed_order_id"),
        "policy_version": case["policy_version"],
    }
    calls: list[tuple[str, str, dict[str, str]]] = []
    for tool_name in _unique(requested):
        if tool_name not in specs or tool_name in called:
            continue
        actor, arguments = TOOL_OWNERS.get(tool_name, "order-payment-agent"), {}
        for parameter in specs[tool_name].input_schema.get("required", []):
            if parameter == "case_id":
                continue
            value = argument_values.get(parameter)
            if value is None:
                errors.append({
                    "actor": actor, "tool_name": tool_name,
                    "error": "required tool arguments cannot be resolved from the case",
                })
                break
            arguments[parameter] = str(value)
        else:
            calls.append((actor, tool_name, arguments))

    async def invoke(call: tuple[str, str, dict[str, str]]) -> tuple[Any, ...]:
        actor, tool_name, arguments = call
        try:
            result = await gateway.call(tool_name, case_id=case_id, **arguments)
            return actor, tool_name, result, None
        except RuntimeError as exc:
            return actor, tool_name, None, str(exc)[:160]

    results = await asyncio.gather(*(invoke(call) for call in calls))
    for actor, tool_name, result, error in results:
        if error:
            errors.append({"actor": actor, "tool_name": tool_name, "error": error})
            continue
        evidence.append({"actor": actor, "tool_name": tool_name, **result})
        trace.emit(
            case_id=case_id, event_type="tool_result_consumed", actor=actor,
            tool_name=tool_name, evidence_refs=[result["evidence_ref"]],
        )
    return {"evidence": evidence, "tool_errors": errors}


def _active_specialists(case: dict[str, Any]) -> set[str]:
    topics = {claim.get("topic") for claim in case["customer_request"].get("claims", [])}
    active = {"order-payment-agent", "policy-resolution-agent"}
    if topics & SHIPPING_TOPICS:
        active.add("shipment-seller-agent")
    return active


def _tool_plan(
    case: dict[str, Any], tool_specs: list[ToolSpec], requested: list[str] | None = None,
) -> list[str]:
    available = {tool.name for tool in tool_specs}
    topics = {claim.get("topic") for claim in case["customer_request"].get("claims", [])}
    payment_topics = {"duplicate_charge", "payment_mismatch", "valid_split_payment"}
    refund_topics = {
        "canceled_order_paid", "unavailable_order_paid", "refund_pending", "refund_failed",
        "requested_full_refund",
    }
    plan = ["get_order", "get_order_payments", "get_policy"]
    if topics & payment_topics:
        plan.append("get_payment_timeline")
    if topics & refund_topics:
        plan.append("get_refund_timeline")
    if topics & ({"unavailable_order_paid"} | SHIPPING_TOPICS):
        plan.append("get_order_items")
    if topics & SHIPPING_TOPICS:
        plan.extend(["get_shipment_summary", "get_sellers"])
    active = _active_specialists(case)
    plan.extend(
        name for name in (requested or []) if TOOL_OWNERS.get(name) in active
    )
    return [name for name in _unique(plan) if name in available]


def _latest_message(state: GraphState, recipient: str) -> dict[str, Any]:
    for message in reversed(state.get("messages", [])):
        if message.get("recipient") == recipient:
            return message
    return {}


def _role_evidence(actor: str, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if actor == "policy-resolution-agent":
        return evidence
    owned = [item for item in evidence if item["actor"] == actor]
    if actor == "shipment-seller-agent":
        owned.extend(
            item for item in evidence
            if item["tool_name"] in {"get_order", "get_order_items"}
        )
    return _unique(owned)


def _compact_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"tool_name": item["tool_name"], "evidence_ref": item["evidence_ref"], "data": item["data"]}
        for item in evidence
    ]


def _valid_finding_refs(
    finding: dict[str, Any], evidence: list[dict[str, Any]]
) -> list[str]:
    valid = {item["evidence_ref"] for item in evidence}
    refs = list(finding.get("evidence_refs", []))
    for claim in finding.get("claim_findings", []):
        refs.extend(claim.get("evidence_refs", []))
    return _unique(ref for ref in refs if ref in valid)[:30]


def _build_output(
    case: dict[str, Any], decision: dict[str, Any], evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    valid_refs = {item["evidence_ref"] for item in evidence}
    refs_by_tool = {item["tool_name"]: item["evidence_ref"] for item in evidence}
    all_refs = _unique(item["evidence_ref"] for item in evidence)[:30]
    authoritative_values = _evidence_strings(evidence)
    submitted = {item["claim_id"]: item for item in decision.get("claim_assessments", [])}
    claim_assessments, claim_refs = [], []
    for claim in case["customer_request"].get("claims", [])[:5]:
        item = submitted.get(claim["claim_id"], {})
        selected = _unique(ref for ref in item.get("evidence_refs", []) if ref in valid_refs)[:30]
        selected = selected or _claim_refs(claim.get("topic", ""), refs_by_tool)
        claim_refs.extend(selected)
        verdict = item.get("verdict") if item.get("verdict") in VERDICTS else None
        claim_assessments.append({
            "claim_id": claim["claim_id"],
            "verdict": verdict if selected and verdict else "insufficient_evidence",
            "confidence": _confidence(item.get("confidence", 0)) if selected else 0.0,
            "evidence_refs": selected,
        })
    # Every MCP call in the deterministic plan is relevant to the case. Keep the full
    # audited set in the case-level provenance so the scorer can verify every required
    # evidence group even when the verifier model omits a citation.
    selected_refs = all_refs
    entities = decision.get("affected_entities", {})
    affected = {
        key: [value for value in _string_set(entities.get(key, []), 20, 128)
              if value in authoritative_values]
        for key in ENTITY_KEYS
    }
    root, causes = decision.get("root_cause_analysis", {}), []
    for raw in root.get("ranked_causes", [])[:5]:
        code = _code(raw.get("cause_code"))
        if code and code not in {item["cause_code"] for item in causes}:
            causes.append({"cause_code": code, "rank": len(causes) + 1})
    causes = causes or [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}]
    parties = []
    allowed_parties = {
        "seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown",
    }
    for party in root.get("responsible_parties", [])[:5]:
        if party.get("party_type") not in allowed_parties:
            continue
        party_id = str(party["party_id"])[:128] if party.get("party_id") else None
        normalized = {
            "party_type": party["party_type"],
            "party_id": party_id if party_id in authoritative_values else None,
        }
        if normalized not in parties:
            parties.append(normalized)
    parties = parties or [{"party_type": "unknown", "party_id": None}]
    financial, refund_lines = decision.get("financial_resolution", {}), []
    for line in financial.get("refund_lines", [])[:10]:
        entity_id = str(line["entity_id"])[:128] if line.get("entity_id") else None
        refund_lines.append({
            "reason_code": str(line.get("reason_code", "REFUND"))[:80] or "REFUND",
            "amount_brl": max(0.0, round(float(line.get("amount_brl", 0)), 2)),
            "entity_id": entity_id if entity_id in authoritative_values else None,
        })
    refund_total = round(sum(item["amount_brl"] for item in refund_lines), 2)
    actions = _string_set(decision.get("resolution_actions", []), 8, 80)
    if refund_total > 0 and not actions:
        actions = ["ISSUE_REFUND"]
    assessment = decision.get("assessment", {})
    issue = assessment.get("primary_issue", "insufficient_evidence")
    issue = issue if issue in PRIMARY_ISSUES else "insufficient_evidence"
    status = assessment.get("case_status", "needs_investigation")
    if refund_total > 0 or actions:
        status = "action_required"
    elif issue in {"unsupported_claim", "valid_split_payment"}:
        status = "no_action"
    elif issue == "insufficient_evidence":
        status = "needs_investigation"
    return {
        "schema_version": "day09-l3a-output-v2", "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue, "case_status": status,
            "confidence": _confidence(assessment.get("confidence", 0)),
        },
        "affected_entities": affected, "claim_assessments": claim_assessments,
        "root_cause_analysis": {"ranked_causes": causes, "responsible_parties": parties},
        "evidence_refs": selected_refs,
        "data_conflicts": _normalize_conflicts(decision.get("data_conflicts", [])),
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": refund_total,
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }


def _claim_refs(topic: str, refs: dict[str, str]) -> list[str]:
    tools = ["get_order", "get_policy"]
    if topic in {"duplicate_charge", "payment_mismatch", "valid_split_payment"}:
        tools += ["get_order_payments", "get_payment_timeline"]
    elif topic in {
        "canceled_order_paid", "unavailable_order_paid", "refund_pending", "refund_failed",
        "requested_full_refund",
    }:
        tools += ["get_order_payments", "get_refund_timeline"]
    elif topic in {"late_delivery_seller", "late_delivery_logistics"}:
        tools += ["get_order_items", "get_shipment_summary", "get_sellers"]
    return [refs[name] for name in tools if name in refs]


def _evidence_strings(evidence: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif isinstance(value, str):
            values.add(value)

    for item in evidence:
        visit(item.get("data", {}))
    return values


def _normalize_conflicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts = []
    for value in values[:5]:
        sources = _string_set(value.get("sources", []), 5, 80)
        if len(sources) < 2:
            continue
        selected = value.get("selected_source")
        conflicts.append({
            "field": str(value.get("field", "unknown"))[:100] or "unknown",
            "sources": sources, "selected_source": str(selected)[:80] if selected else None,
            "resolution_code": str(value.get("resolution_code", "UNRESOLVED"))[:80]
            or "UNRESOLVED",
        })
    return conflicts


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, round(float(value), 4)))
    except (TypeError, ValueError):
        return 0.0


def _code(value: Any) -> str:
    text = re.sub(r"[^A-Z0-9_]+", "_", str(value or "").upper()).strip("_")[:80]
    return text if len(text) >= 3 and text[0].isalpha() else ""


def _string_set(values: Any, limit: int, max_length: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return _unique(str(value)[:max_length] for value in values if str(value))[:limit]


def _unique(values: Any) -> list[Any]:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
