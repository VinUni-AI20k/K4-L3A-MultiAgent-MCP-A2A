from __future__ import annotations

from typing import Any

from .state import CaseGraphState


def _values(case: dict[str, Any], *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        raw = case.get(name, [])
        for item in raw if isinstance(raw, list) else [raw]:
            if isinstance(item, str) and item and item not in values:
                values.append(item)
    return values


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return str(value).lower()


async def coordinator_node(state: CaseGraphState) -> dict[str, Any]:
    case = state["case"]
    entities = {
        "order_ids": _values(case, "order_id", "order_ids"),
        "item_ids": _values(case, "item_id", "item_ids", "order_item_id"),
        "seller_ids": _values(case, "seller_id", "seller_ids"),
        "payment_references": _values(
            case, "payment_reference", "payment_references", "payment_id"
        ),
        "shipment_ids": _values(case, "shipment_id", "shipment_ids"),
    }
    trace = state["trace"]
    for actor in ("order-item-agent", "payment-agent", "shipment-agent"):
        trace.emit(
            case_id=state["case_id"],
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
        )
    return {"entities": entities, "evidence": {}, "evidence_refs": []}


async def _collect(
    state: CaseGraphState, *, actor: str, tools: tuple[str, ...]
) -> dict[str, Any]:
    order_ids = state["entities"]["order_ids"]
    if not order_ids:
        return {}
    evidence_by_tool = dict(state.get("evidence", {}))
    refs = list(state.get("evidence_refs", []))
    for tool in tools:
        try:
            evidence = await state["gateway"].call(
                tool, case_id=state["case_id"], actor=actor, order_id=order_ids[0]
            )
        except (RuntimeError, ValueError):
            # Gateway already performs its single, timeout-only retry.  A failed
            # specialist task is represented by absent evidence, never invented data.
            continue
        ref = evidence["evidence_ref"]
        evidence_by_tool[tool] = evidence
        if ref not in refs:
            refs.append(ref)
        state["trace"].emit(
            case_id=state["case_id"],
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool, evidence_refs=[ref],
        )
        state["trace"].emit(
            case_id=state["case_id"],
            event_type="handoff",
            actor=actor,
            target="policy-agent", evidence_refs=[ref],
        )
    return {"evidence": evidence_by_tool, "evidence_refs": refs}


async def order_node(state: CaseGraphState) -> dict[str, Any]:
    return await _collect(
        state, actor="order-item-agent", tools=("get_order", "get_order_items")
    )


async def payment_node(state: CaseGraphState) -> dict[str, Any]:
    return await _collect(state, actor="payment-agent", tools=("get_order_payments",))


async def shipment_node(state: CaseGraphState) -> dict[str, Any]:
    return await _collect(state, actor="shipment-agent", tools=("get_shipment_summary",))


async def policy_node(state: CaseGraphState) -> dict[str, Any]:
    text = _text(state["case"]) + " " + _text(
        [item.get("data") for item in state.get("evidence", {}).values()]
    )
    if "duplicate" in text:
        issue, action = "duplicate_charge", "review_duplicate_charge"
    elif "refund" in text and ("pending" in text or "await" in text):
        issue, action = "refund_pending", "monitor_refund"
    elif "cancel" in text and "paid" in text:
        issue, action = "canceled_order_paid", "issue_refund"
    elif "late" in text or "delay" in text:
        issue, action = "late_delivery_logistics", "escalate_delivery"
    else:
        issue, action = "insufficient_evidence", "collect_missing_evidence"
    state["trace"].emit(
        case_id=state["case_id"],
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=issue, evidence_refs=state.get("evidence_refs", []),
    )
    return {"primary_issue": issue, "resolution_action": action}


async def verifier_node(state: CaseGraphState) -> dict[str, Any]:
    issue = state["primary_issue"]
    refs = list(dict.fromkeys(state.get("evidence_refs", [])))
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": state["case_id"],
        "assessment": {
            "primary_issue": issue,
            "case_status": (
                "action_required" if issue != "insufficient_evidence" else "needs_investigation"
            ),
            "confidence": (
                min(0.95, 0.35 + 0.12 * len(refs))
                if issue != "insufficient_evidence"
                else 0.0
            ),
        },
        "affected_entities": state["entities"],
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": [state["resolution_action"]],
    }
    state["trace"].emit(
        case_id=state["case_id"],
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="schema_ready", evidence_refs=refs,
    )
    return {"output": output}
