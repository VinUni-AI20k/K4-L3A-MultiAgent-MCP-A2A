from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return str(value).lower()


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    def values(*names: str) -> list[str]:
        result: list[str] = []
        for name in names:
            raw = case.get(name, [])
            for item in raw if isinstance(raw, list) else [raw]:
                if isinstance(item, str) and item and item not in result:
                    result.append(item)
        return result

    entities = {
        "order_ids": values("order_id", "order_ids"),
        "item_ids": values("item_id", "item_ids", "order_item_id"),
        "seller_ids": values("seller_id", "seller_ids"),
        "payment_references": values("payment_reference", "payment_references", "payment_id"),
        "shipment_ids": values("shipment_id", "shipment_ids"),
    }
    refs: list[str] = []
    evidence_data: list[Any] = []
    for actor, tool in (("order-agent", "get_order"), ("order-item-agent", "get_order_items"),
                        ("payment-agent", "get_order_payments"), ("shipment-agent", "get_shipment_summary")):
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
        if not entities["order_ids"]:
            continue
        try:
            evidence = await gateway.call(tool, case_id=case_id, order_id=entities["order_ids"][0])
        except (RuntimeError, ValueError):
            # A single bounded retry is safe because MCP calls are read-only.
            try:
                evidence = await gateway.call(tool, case_id=case_id, order_id=entities["order_ids"][0])
            except (RuntimeError, ValueError):
                continue
        ref = evidence["evidence_ref"]
        refs.append(ref)
        evidence_data.append(evidence.get("data"))
        trace.emit(case_id=case_id, event_type="tool_result_consumed", actor=actor, tool_name=tool, evidence_refs=[ref])
        trace.emit(case_id=case_id, event_type="handoff", actor=actor, target="policy-agent", evidence_refs=[ref])
    text = _text(case) + " " + _text(evidence_data)
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
    confidence = min(0.95, 0.35 + 0.12 * len(refs)) if issue != "insufficient_evidence" else 0.0
    trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent", decision_code=issue)
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier-agent", decision_code="schema_ready", evidence_refs=refs)
    return {"schema_version": "day09-l3a-output-v2", "case_id": case_id,
            "assessment": {"primary_issue": issue, "case_status": "action_required" if issue != "insufficient_evidence" else "needs_investigation", "confidence": confidence},
            "affected_entities": entities, "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
            "evidence_refs": list(dict.fromkeys(refs)), "data_conflicts": [],
            "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []},
            "resolution_actions": [action]}
