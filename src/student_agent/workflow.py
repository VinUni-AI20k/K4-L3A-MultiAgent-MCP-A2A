"""Observable, evidence-first A2A workflow for the L3A variant.

This module deliberately uses a small async state machine instead of a framework.
The public contracts, not an agent prompt, define the boundary of every final answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx2

from . import OUTPUT_SCHEMA_VERSION
from .cases import CASE_ID_PATTERN
from .mcp_gateway import EvidenceGateway
from .policy_engine import decide, verify
from .trace import TraceWriter

MAX_MCP_ATTEMPTS = 2


@dataclass(frozen=True)
class ToolRequest:
    """A narrowly scoped request a specialist is permitted to make."""

    tool_name: str
    arguments: dict[str, str]


@dataclass
class InvestigationState:
    case_id: str
    discovered_tools: set[str]
    evidence_by_domain: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    evidence_by_tool: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    failed_requests: int = 0

    def record(self, tool_name: str, evidence: dict[str, Any]) -> None:
        self.evidence_by_domain.setdefault(str(evidence["domain"]), []).append(evidence)
        self.evidence_by_tool.setdefault(tool_name, []).append(evidence)


def _strings_for_keys(value: Any, keys: set[str]) -> list[str]:
    """Read explicit identifiers from the input without inferring identifiers."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, str) and child:
                found.append(child)
            else:
                found.extend(_strings_for_keys(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(_strings_for_keys(child, keys))
    return list(dict.fromkeys(found))


def _requests(case: dict[str, Any], allowed_tools: Iterable[str]) -> list[ToolRequest]:
    """Build requests only for tools that MCP discovery returned.

    Argument names match the documented gateway example. A request is not attempted
    unless its matching identifier is explicitly present in the case.
    """
    allowed = set(allowed_tools)
    request_specs = (
        ("get_order", "order_id", {"order_id", "claimed_order_id"}),
        ("get_order_items", "order_id", {"order_id", "claimed_order_id"}),
        ("get_order_payments", "order_id", {"order_id", "claimed_order_id"}),
        ("get_payment_timeline", "order_id", {"order_id", "claimed_order_id"}),
        ("get_refund_timeline", "order_id", {"order_id", "claimed_order_id"}),
        ("get_shipment_summary", "order_id", {"order_id", "claimed_order_id"}),
        ("get_policy", "policy_version", {"policy_version"}),
    )
    result: list[ToolRequest] = []
    for tool_name, argument_name, keys in request_specs:
        if tool_name not in allowed:
            continue
        result.extend(
            ToolRequest(tool_name, {argument_name: identifier})
            for identifier in _strings_for_keys(case, keys)
        )
    return result


async def _collect(
    *,
    actor: str,
    state: InvestigationState,
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    permitted_tools: set[str],
    handoff_target: str = "policy-agent",
) -> None:
    """Collect and trace validated MCP envelopes for one specialist."""
    trace.emit(
        case_id=state.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        decision_code="EVIDENCE_COLLECTION_ASSIGNED",
    )
    for request in _requests(case, permitted_tools & state.discovered_tools):
        for attempt in range(1, MAX_MCP_ATTEMPTS + 1):
            try:
                evidence = await gateway.call(request.tool_name, case_id=state.case_id, **request.arguments)
            except (RuntimeError, ValueError, OSError, httpx2.HTTPError):
                if attempt == MAX_MCP_ATTEMPTS:
                    state.failed_requests += 1
                else:
                    await asyncio.sleep(0.2 * attempt)
                continue
            state.record(request.tool_name, evidence)
            trace.emit(
                case_id=state.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=request.tool_name,
                evidence_refs=[str(evidence["evidence_ref"])],
            )
            break
    trace.emit(
        case_id=state.case_id,
        event_type="handoff",
        actor=actor,
        target=handoff_target,
        decision_code="SPECIALIST_EVIDENCE_READY",
    )


def _safe_insufficient_evidence_output(case_id: str) -> dict[str, Any]:
    """Return a schema-valid abstention; never manufacture an evidence reference."""
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [], "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": [],
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": 0, "refund_lines": [],
        },
        "resolution_actions": ["collect_additional_evidence"],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate specialists and return only a contract-valid final result.

    Phase 2 establishes A2A boundaries and the evidence lifecycle. Specialist
    evidence is intentionally not converted into business conclusions until domain
    extraction rules can link every claim to supporting evidence.
    """
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError("case.case_id must be a valid Day09 case ID")

    state = InvestigationState(case_id=case_id, discovered_tools=set(await gateway.list_tools()))
    specialists = (
        ("order-item-agent", {"get_order", "get_order_items"}),
        ("payment-agent", {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}),
        ("shipment-agent", {"get_shipment_summary"}),
    )
    for actor, permissions in specialists:
        await _collect(
            actor=actor,
            state=state,
            case=case,
            gateway=gateway,
            trace=trace,
            permitted_tools=permissions,
        )

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
        decision_code="POLICY_REVIEW_ASSIGNED",
    )
    await _collect(
        actor="policy-agent",
        state=state,
        case=case,
        gateway=gateway,
        trace=trace,
        permitted_tools={"get_policy"},
        handoff_target="verifier-agent",
    )
    decision = decide(case, state.evidence_by_tool)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier-agent",
        decision_code=f"POLICY_{decision['issue'].upper()}",
        evidence_refs=decision["refs"],
        attributes={"mcp_failures": state.failed_requests},
    )
    output = verify(case_id, decision, OUTPUT_SCHEMA_VERSION)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code=(
            "EVIDENCE_BACKED_DECISION"
            if output["assessment"]["primary_issue"] != "insufficient_evidence"
            else "SCHEMA_SAFE_ABSTENTION"
        ),
    )
    return output
