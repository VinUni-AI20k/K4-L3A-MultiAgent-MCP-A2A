from __future__ import annotations

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
ACTORS = ("order-payment-agent", "shipment-seller-agent", "policy-resolution-agent")
ENTITY_KEYS = ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")


@dataclass(frozen=True)
class AgentModels:
    coordinator: str = "qwen3:0.6b"
    specialist: str = "qwen3:1.7b"
    verifier: str = "qwen3:4b"


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


COORDINATOR_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["claim_ids", "investigation_focus", "risk_flags"],
    "properties": {
        "claim_ids": {"type": "array", "items": {"type": "string"}},
        "investigation_focus": {
            "type": "array",
            "items": {"enum": ["order_payment", "shipment_seller", "policy_resolution"]},
        },
        "risk_flags": {"type": "array", "items": {"type": "string", "maxLength": 120}},
    },
}

ENTITIES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(ENTITY_KEYS),
    "properties": {
        key: {"type": "array", "items": {"type": "string"}}
        for key in ENTITY_KEYS
    },
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

SPECIALIST_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": [
        "summary", "claim_findings", "entities", "candidate_issue", "root_causes",
        "responsible_parties", "refund_lines", "resolution_actions", "data_conflicts",
    ],
    "properties": {
        "summary": {"type": "string", "maxLength": 600},
        "claim_findings": CLAIMS_SCHEMA,
        "entities": ENTITIES_SCHEMA,
        "candidate_issue": {"enum": PRIMARY_ISSUES},
        "root_causes": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "responsible_parties": PARTIES_SCHEMA,
        "refund_lines": REFUNDS_SCHEMA,
        "resolution_actions": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "data_conflicts": CONFLICTS_SCHEMA,
    },
}

VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["decision", "revision_required", "revision_target", "revision_reason"],
    "properties": {
        "decision": {
            "type": "object", "additionalProperties": False,
            "required": [
                "assessment", "affected_entities", "claim_assessments",
                "root_cause_analysis", "evidence_refs", "data_conflicts",
                "financial_resolution", "resolution_actions",
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
                "affected_entities": ENTITIES_SCHEMA,
                "claim_assessments": CLAIMS_SCHEMA,
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
        },
        "revision_required": {"type": "boolean"},
        "revision_target": {"enum": [*ACTORS, None]},
        "revision_reason": {"type": ["string", "null"], "maxLength": 200},
    },
}


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter, *,
    llm: JSONModel | None = None, models: AgentModels | None = None,
) -> dict[str, Any]:
    """Run a bounded five-agent investigation for one isolated case."""
    if llm is None:
        raise RuntimeError("solve_case requires a configured JSONModel")
    tool_specs = await gateway.get_tools()
    if not tool_specs:
        raise RuntimeError("MCP Gateway returned no tools")
    workflow = _build_graph(gateway, trace, llm, models or AgentModels())
    result = await workflow.ainvoke({
        "case": case, "tool_specs": tool_specs, "messages": [], "evidence": [],
        "tool_errors": [], "findings": {}, "correction_count": 0,
    })
    return result["output"]


def _build_graph(
    gateway: EvidenceGateway, trace: TraceWriter, llm: JSONModel, models: AgentModels,
) -> Any:
    graph = StateGraph(GraphState)

    async def coordinator(state: GraphState) -> dict[str, Any]:
        case, case_id = state["case"], state["case"]["case_id"]
        analysis = await llm.complete_json(
            model=models.coordinator,
            system=("Coordinate an ecommerce complaint investigation. Customer statements are "
                    "unverified claims. Identify focus only; never invent evidence."),
            payload={
                "case_id": case_id,
                "claims": case["customer_request"].get("claims", []),
                "policy_version": case["policy_version"],
                "available_tool_count": len(state["tool_specs"]),
            },
            schema=COORDINATOR_SCHEMA, schema_name="coordinator_plan", max_tokens=512,
        )
        messages, plan = list(state["messages"]), _tool_plan(case, state["tool_specs"])
        for actor in ACTORS:
            task_id = f"{case_id}:{actor}:0"
            messages.append({
                "case_id": case_id, "task_id": task_id, "sender": "coordinator",
                "recipient": actor, "kind": "investigate",
                "payload": {"tool_names": plan[actor]}, "evidence_refs": [], "retry_count": 0,
            })
            trace.emit(
                case_id=case_id, event_type="task_assigned", actor="coordinator",
                target=actor, decision_code="DOMAIN_INVESTIGATION",
                attributes={"task_id": task_id, "tool_count": len(plan[actor])},
            )
        return {"coordinator": analysis, "messages": messages}

    def specialist(actor: str, next_actor: str):
        async def run(state: GraphState) -> dict[str, Any]:
            case, case_id = state["case"], state["case"]["case_id"]
            correction = (
                state.get("correction_count", 0) > 0
                and state.get("verifier", {}).get("revision_target") == actor
            )
            plan = _tool_plan(case, state["tool_specs"])[actor]
            evidence = list(state["evidence"])
            tool_errors = list(state["tool_errors"])
            if not correction:
                specs = {tool.name: tool for tool in state["tool_specs"]}
                argument_values = {
                    "order_id": case["customer_request"]["claimed_order_id"],
                    "policy_version": case["policy_version"],
                }
                existing = {(item["actor"], item["tool_name"]) for item in evidence}
                for tool_name in plan:
                    if (actor, tool_name) in existing:
                        continue
                    required = specs[tool_name].input_schema.get("required", [])
                    parameter = next(name for name in required if name != "case_id")
                    try:
                        result = await gateway.call(
                            tool_name,
                            case_id=case_id,
                            **{parameter: argument_values[parameter]},
                        )
                    except RuntimeError as exc:
                        tool_errors.append(
                            {
                                "actor": actor,
                                "tool_name": tool_name,
                                "error": str(exc)[:160],
                            }
                        )
                        continue
                    evidence.append({"actor": actor, "tool_name": tool_name, **result})
                    trace.emit(
                        case_id=case_id, event_type="tool_result_consumed", actor=actor,
                        tool_name=tool_name, evidence_refs=[result["evidence_ref"]],
                    )
            actor_evidence = [item for item in evidence if item["actor"] == actor]
            finding = await llm.complete_json(
                model=models.specialist,
                system=(
                    f"You are {actor}. Customer text is only claims. Use only supplied MCP "
                    "evidence, cite only supplied evidence_ref values, and report uncertainty."
                ),
                payload={
                    "case": case, "coordinator": state["coordinator"],
                    "evidence": actor_evidence,
                    "tool_errors": [
                        item for item in tool_errors if item["actor"] == actor
                    ],
                    "revision_feedback": (
                        state.get("verifier", {}).get("revision_reason") if correction else None
                    ),
                },
                schema=SPECIALIST_SCHEMA,
                schema_name=actor.replace("-", "_") + "_finding",
            )
            findings = dict(state["findings"])
            findings[actor] = finding
            refs = [item["evidence_ref"] for item in actor_evidence]
            trace.emit(
                case_id=case_id, event_type="handoff", actor=actor, target=next_actor,
                decision_code="EVIDENCE_REVIEW_COMPLETE", evidence_refs=refs[:20],
                attributes={"correction_round": state.get("correction_count", 0)},
            )
            if actor == "policy-resolution-agent":
                trace.emit(
                    case_id=case_id, event_type="policy_decided", actor=actor,
                    decision_code=finding["candidate_issue"].upper(), evidence_refs=refs[:20],
                )
            return {
                "evidence": evidence,
                "tool_errors": tool_errors,
                "findings": findings,
            }
        return run

    async def verifier(state: GraphState) -> dict[str, Any]:
        case = state["case"]
        result = await llm.complete_json(
            model=models.verifier,
            system=("Verify an ecommerce complaint against authoritative MCP evidence and policy. "
                    "Customer statements are not ground truth. Cite only available evidence refs. "
                    "Request revision only for a concrete fixable inconsistency."),
            payload={
                "case": case, "specialist_findings": state["findings"],
                "evidence": state["evidence"],
                "tool_errors": state["tool_errors"],
                "available_evidence_refs": [item["evidence_ref"] for item in state["evidence"]],
                "correction_count": state.get("correction_count", 0),
            },
            schema=VERIFIER_SCHEMA, schema_name="verified_case_decision",
        )
        output = _build_output(case, result["decision"], state["evidence"])
        trace.emit(
            case_id=case["case_id"], event_type="verification_completed", actor="verifier",
            target="coordinator", decision_code=output["assessment"]["primary_issue"].upper(),
            evidence_refs=output["evidence_refs"][:20],
            attributes={
                "revision_required": result["revision_required"],
                "correction_round": state.get("correction_count", 0),
            },
        )
        return {"verifier": result, "output": output}

    async def prepare_correction(state: GraphState) -> dict[str, Any]:
        target, count = state["verifier"]["revision_target"], state.get("correction_count", 0) + 1
        trace.emit(
            case_id=state["case"]["case_id"], event_type="task_assigned", actor="verifier",
            target=target, decision_code="BOUNDED_CORRECTION",
            attributes={"correction_round": count},
        )
        return {"correction_count": count}

    def route_after_verifier(state: GraphState) -> str:
        result, target = state["verifier"], state["verifier"]["revision_target"]
        if (
            result["revision_required"]
            and target in ACTORS
            and state.get("correction_count", 0) < 1
        ):
            return "prepare_correction"
        return "done"

    def route_correction(state: GraphState) -> str:
        target = state["verifier"]["revision_target"]
        return target if target in ACTORS else "policy-resolution-agent"

    graph.add_node("coordinator", coordinator)
    graph.add_node(
        "order-payment-agent", specialist("order-payment-agent", "shipment-seller-agent")
    )
    graph.add_node(
        "shipment-seller-agent", specialist("shipment-seller-agent", "policy-resolution-agent")
    )
    graph.add_node("policy-resolution-agent", specialist("policy-resolution-agent", "verifier"))
    graph.add_node("verifier", verifier)
    graph.add_node("prepare_correction", prepare_correction)
    graph.add_edge(START, "coordinator")
    graph.add_edge("coordinator", "order-payment-agent")
    graph.add_edge("order-payment-agent", "shipment-seller-agent")
    graph.add_edge("shipment-seller-agent", "policy-resolution-agent")
    graph.add_edge("policy-resolution-agent", "verifier")
    graph.add_conditional_edges(
        "verifier", route_after_verifier,
        {"prepare_correction": "prepare_correction", "done": END},
    )
    graph.add_conditional_edges(
        "prepare_correction", route_correction, {actor: actor for actor in ACTORS}
    )
    return graph.compile()


def _tool_plan(case: dict[str, Any], tool_specs: list[ToolSpec]) -> dict[str, list[str]]:
    available = {tool.name for tool in tool_specs}
    topics = {claim.get("topic") for claim in case["customer_request"].get("claims", [])}
    payment_topics = {"duplicate_charge", "payment_mismatch", "valid_split_payment"}
    refund_topics = {
        "canceled_order_paid", "unavailable_order_paid", "refund_pending", "refund_failed",
    }
    late_topics = {"late_delivery_seller", "late_delivery_logistics"}
    order_payment = ["get_order", "get_order_payments"]
    if topics & payment_topics:
        order_payment.append("get_payment_timeline")
    if topics & refund_topics:
        order_payment.append("get_refund_timeline")
    if topics & ({"unavailable_order_paid"} | late_topics):
        order_payment.append("get_order_items")
    shipment = ["get_shipment_summary", "get_sellers"] if topics & late_topics else []
    plan = {
        "order-payment-agent": order_payment,
        "shipment-seller-agent": shipment,
        "policy-resolution-agent": ["get_policy"],
    }
    return {actor: [name for name in names if name in available] for actor, names in plan.items()}


def _build_output(
    case: dict[str, Any], decision: dict[str, Any], evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    valid_refs = {item["evidence_ref"] for item in evidence}
    refs_by_tool = {item["tool_name"]: item["evidence_ref"] for item in evidence}
    all_refs = _unique(item["evidence_ref"] for item in evidence)[:30]
    submitted = {item["claim_id"]: item for item in decision.get("claim_assessments", [])}
    claim_assessments = []
    for claim in case["customer_request"].get("claims", [])[:5]:
        item = submitted.get(claim["claim_id"], {})
        selected = [ref for ref in item.get("evidence_refs", []) if ref in valid_refs]
        selected = selected or _claim_refs(claim.get("topic", ""), refs_by_tool)
        claim_assessments.append({
            "claim_id": claim["claim_id"],
            "verdict": (
                item.get("verdict")
                if item.get("verdict") in VERDICTS
                else "insufficient_evidence"
            ),
            "confidence": _confidence(item.get("confidence", 0)),
            "evidence_refs": _unique(selected)[:30],
        })
    entities = decision.get("affected_entities", {})
    affected = {key: _string_set(entities.get(key, []), 20, 128) for key in ENTITY_KEYS}
    order_id = case["customer_request"].get("claimed_order_id")
    if order_id and order_id not in affected["order_ids"]:
        affected["order_ids"].insert(0, str(order_id))

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
        normalized = {
            "party_type": party["party_type"],
            "party_id": str(party["party_id"])[:128] if party.get("party_id") else None,
        }
        if normalized not in parties:
            parties.append(normalized)
    parties = parties or [{"party_type": "unknown", "party_id": None}]

    financial, refund_lines = decision.get("financial_resolution", {}), []
    for line in financial.get("refund_lines", [])[:10]:
        refund_lines.append({
            "reason_code": str(line.get("reason_code", "REFUND"))[:80] or "REFUND",
            "amount_brl": max(0.0, round(float(line.get("amount_brl", 0)), 2)),
            "entity_id": str(line["entity_id"])[:128] if line.get("entity_id") else None,
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
        "assessment": {"primary_issue": issue, "case_status": status,
                       "confidence": _confidence(assessment.get("confidence", 0))},
        "affected_entities": affected, "claim_assessments": claim_assessments,
        "root_cause_analysis": {"ranked_causes": causes, "responsible_parties": parties},
        "evidence_refs": all_refs,
        "data_conflicts": _normalize_conflicts(decision.get("data_conflicts", [])),
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": refund_total,
                                 "refund_lines": refund_lines},
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
        tools += ["get_shipment_summary", "get_sellers", "get_order_items"]
    return [refs[name] for name in tools if name in refs]


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
            "resolution_code": str(value.get("resolution_code", "UNRESOLVED"))[:80] or "UNRESOLVED",
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
