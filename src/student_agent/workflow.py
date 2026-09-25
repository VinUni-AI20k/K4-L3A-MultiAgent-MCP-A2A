"""
L3A Multi-Agent Workflow
========================
Architecture:
    Coordinator → [Order/Item Agent, Payment Agent, Shipment Agent]
                                    ↓ (MCP Evidence)
                               Policy Agent
                                    ↓
                             Verifier Agent → Output
LLM Backend: NVIDIA NIM — nvidia/nemotron-3-ultra-550b-a55b
             (OpenAI-compatible API at integrate.api.nvidia.com)
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any

from openai import OpenAI

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# ---------------------------------------------------------------------------
# LLM client (NVIDIA NIM — OpenAI-compatible)
# ---------------------------------------------------------------------------
_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
_BASE_URL = "https://integrate.api.nvidia.com/v1"


def _get_client() -> OpenAI:
    api_key = os.getenv("NVIDIA_API_KEY", "")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY is not set in .env")
    return OpenAI(base_url=_BASE_URL, api_key=api_key)


def _llm_json(client: OpenAI, system: str, user: str) -> Any:
    """Call NVIDIA NIM and parse JSON, with retry on 429/503."""
    last_exc: Exception | None = None
    for attempt in range(6):
        try:
            completion = client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": user + "\n\nRespond with ONLY valid JSON, no markdown fences.",
                    },
                ],
                temperature=0.1,
                max_tokens=2048,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            content = completion.choices[0].message.content
            if not content:
                raise ValueError("NVIDIA NIM returned empty content")
            raw = content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()
            transient_markers = ("429", "503", "rate", "unavailable", "overload")
            if any(marker in err_str for marker in transient_markers):
                wait = min(2 ** attempt * 10, 120)
                print(
                    f"[NVIDIA] {exc.__class__.__name__} attempt {attempt + 1}/6, "
                    f"retry in {wait}s..."
                )
                time.sleep(wait)
            elif isinstance(exc, json.JSONDecodeError) or isinstance(exc, ValueError):
                print(f"[NVIDIA] Invalid JSON or empty response attempt {attempt+1}/6, retrying...")
                time.sleep(2)
            else:
                raise
    raise RuntimeError(f"NVIDIA NIM failed after 6 attempts: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Agent: Order / Item
# ---------------------------------------------------------------------------
async def _order_item_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    client: OpenAI,
) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]

    order_ev = await gateway.call("get_order", case_id=case_id, order_id=order_id)
    trace.emit(
        case_id=case_id, event_type="tool_result_consumed",
        actor="order-agent", tool_name="get_order",
        evidence_refs=[order_ev["evidence_ref"]],
    )

    items_ev = await gateway.call("get_order_items", case_id=case_id, order_id=order_id)
    trace.emit(
        case_id=case_id, event_type="tool_result_consumed",
        actor="order-agent", tool_name="get_order_items",
        evidence_refs=[items_ev["evidence_ref"]],
    )

    system = (
        "You are an order/item specialist agent for an e-commerce dispute platform. "
        "Analyse the order and item evidence and return a concise JSON summary."
    )
    user = (
        f"Case ID: {case_id}\n"
        f"Customer request: {json.dumps(case['customer_request'], ensure_ascii=False)}\n"
        f"Order evidence: {json.dumps(order_ev['data'], ensure_ascii=False)}\n"
        f"Items evidence: {json.dumps(items_ev['data'], ensure_ascii=False)}\n\n"
        "Return JSON with keys: order_status (str), items_summary (list of str), "
        "order_ids (list of str), item_ids (list of str), seller_ids (list of str), "
        "anomalies (list of str)."
    )
    analysis = _llm_json(client, system, user)

    return {
        "evidence_refs": [order_ev["evidence_ref"], items_ev["evidence_ref"]],
        "analysis": analysis,
    }


# ---------------------------------------------------------------------------
# Agent: Payment
# ---------------------------------------------------------------------------
async def _payment_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    client: OpenAI,
    order_result: dict[str, Any],
) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]

    payments_ev = await gateway.call("get_order_payments", case_id=case_id, order_id=order_id)
    trace.emit(
        case_id=case_id, event_type="tool_result_consumed",
        actor="payment-agent", tool_name="get_order_payments",
        evidence_refs=[payments_ev["evidence_ref"]],
    )

    timeline_ev = await gateway.call("get_payment_timeline", case_id=case_id, order_id=order_id)
    trace.emit(
        case_id=case_id, event_type="tool_result_consumed",
        actor="payment-agent", tool_name="get_payment_timeline",
        evidence_refs=[timeline_ev["evidence_ref"]],
    )

    refund_ev_refs: list[str] = []
    refund_data: Any = {}
    try:
        refund_ev = await gateway.call("get_refund_timeline", case_id=case_id, order_id=order_id)
        trace.emit(
            case_id=case_id, event_type="tool_result_consumed",
            actor="payment-agent", tool_name="get_refund_timeline",
            evidence_refs=[refund_ev["evidence_ref"]],
        )
        refund_ev_refs = [refund_ev["evidence_ref"]]
        refund_data = refund_ev["data"]
    except RuntimeError as e:
        print(f"[{case_id}] get_refund_timeline unavailable: {e}")

    system = (
        "You are a payment specialist agent. Analyse payment, timeline and refund evidence "
        "for an e-commerce dispute. Return a concise JSON summary."
    )
    user = (
        f"Case ID: {case_id}\n"
        f"Order analysis: {json.dumps(order_result['analysis'], ensure_ascii=False)}\n"
        f"Payments: {json.dumps(payments_ev['data'], ensure_ascii=False)}\n"
        f"Payment timeline: {json.dumps(timeline_ev['data'], ensure_ascii=False)}\n"
        f"Refund timeline: {json.dumps(refund_data, ensure_ascii=False)}\n\n"
        "Return JSON with keys: total_paid_brl (number), payment_references (list of str), "
        "refund_status (str), recommended_refund_brl (number), "
        "refund_lines (list of {reason_code, amount_brl, entity_id}), "
        "anomalies (list of str)."
    )
    analysis = _llm_json(client, system, user)

    return {
        "evidence_refs": [
            payments_ev["evidence_ref"],
            timeline_ev["evidence_ref"],
            *refund_ev_refs,
        ],
        "analysis": analysis,
    }


# ---------------------------------------------------------------------------
# Agent: Shipment
# ---------------------------------------------------------------------------
async def _shipment_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    client: OpenAI,
) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]

    ship_ev_refs: list[str] = []
    ship_data: Any = {}
    try:
        ship_ev = await gateway.call("get_shipment_summary", case_id=case_id, order_id=order_id)
        trace.emit(
            case_id=case_id, event_type="tool_result_consumed",
            actor="shipment-agent", tool_name="get_shipment_summary",
            evidence_refs=[ship_ev["evidence_ref"]],
        )
        ship_ev_refs = [ship_ev["evidence_ref"]]
        ship_data = ship_ev["data"]
    except RuntimeError as e:
        print(f"[{case_id}] get_shipment_summary unavailable: {e}")

    system = (
        "You are a shipment specialist agent. Analyse shipment evidence for an e-commerce dispute."
    )
    user = (
        f"Case ID: {case_id}\n"
        f"Shipment evidence: {json.dumps(ship_data, ensure_ascii=False)}\n\n"
        "Return JSON with keys: shipment_ids (list of str), delivery_status (str), "
        "late_delivery (bool), "
        "responsible_for_delay (one of: seller/logistics_provider/platform/unknown), "
        "anomalies (list of str)."
    )
    analysis = _llm_json(client, system, user)

    return {
        "evidence_refs": ship_ev_refs,
        "analysis": analysis,
    }


# ---------------------------------------------------------------------------
# Agent: Policy
# ---------------------------------------------------------------------------
async def _policy_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    client: OpenAI,
    order_result: dict[str, Any],
    payment_result: dict[str, Any],
    shipment_result: dict[str, Any],
) -> dict[str, Any]:
    case_id = case["case_id"]
    policy_version = case.get("policy_version", "EC_POLICY_V1")

    policy_ev = await gateway.call("get_policy", case_id=case_id, policy_version=policy_version)
    trace.emit(
        case_id=case_id, event_type="tool_result_consumed",
        actor="policy-agent", tool_name="get_policy",
        evidence_refs=[policy_ev["evidence_ref"]],
    )

    system = (
        "You are a policy specialist agent. Apply platform policy to the gathered evidence "
        "and determine the correct resolution for the e-commerce dispute."
    )
    user = (
        f"Case ID: {case_id}\n"
        f"Claims: {json.dumps(case['customer_request'].get('claims', []), ensure_ascii=False)}\n"
        f"Order analysis: {json.dumps(order_result['analysis'], ensure_ascii=False)}\n"
        f"Payment analysis: {json.dumps(payment_result['analysis'], ensure_ascii=False)}\n"
        f"Shipment analysis: {json.dumps(shipment_result['analysis'], ensure_ascii=False)}\n"
        f"Policy: {json.dumps(policy_ev['data'], ensure_ascii=False)}\n\n"
        "Return JSON with these exact keys:\n"
        "  primary_issue: one of [canceled_order_paid, unavailable_order_paid, "
        "late_delivery_seller, late_delivery_logistics, valid_split_payment, "
        "payment_mismatch, duplicate_charge, refund_pending, refund_failed, "
        "unsupported_claim, insufficient_evidence]\n"
        "  case_status: one of [action_required, no_action, needs_investigation]\n"
        "  confidence: float 0-1\n"
        "  responsible_parties: list of {party_type, party_id} where party_type is one of "
        "[seller, platform, logistics_provider, payment_provider, customer, unknown]\n"
        "  ranked_causes: list of {cause_code (UPPER_SNAKE), rank (int 1-5)}\n"
        "  claim_assessments: list of {claim_id, verdict (supported/unsupported/"
        "partially_supported/insufficient_evidence), confidence (float), evidence_refs (list)}\n"
        "  data_conflicts: list of {field, sources (list min 2), selected_source, "
        "resolution_code}\n"
        "  resolution_actions: list of str (max 8, each max 80 chars)"
    )
    analysis = _llm_json(client, system, user)

    return {
        "evidence_refs": [policy_ev["evidence_ref"]],
        "analysis": analysis,
    }


# ---------------------------------------------------------------------------
# Agent: Verifier (pure Python — no LLM)
# ---------------------------------------------------------------------------
def _verifier_agent(
    case: dict[str, Any],
    order_result: dict[str, Any],
    payment_result: dict[str, Any],
    shipment_result: dict[str, Any],
    policy_result: dict[str, Any],
) -> dict[str, Any]:
    """Sanitize agent data into the public output contract."""
    case_id = case["case_id"]

    all_refs: list[str] = []
    for res in [order_result, payment_result, shipment_result, policy_result]:
        all_refs.extend(res.get("evidence_refs", []))
    unique_refs = _unique_strings(all_refs, 30, 99)

    pa = policy_result["analysis"]
    oa = order_result["analysis"]
    pay = payment_result["analysis"]
    ship = shipment_result["analysis"]

    affected_entities = {
        "order_ids": _safe_str_list(oa.get("order_ids", [])),
        "item_ids": _safe_str_list(oa.get("item_ids", [])),
        "seller_ids": _safe_str_list(oa.get("seller_ids", [])),
        "payment_references": _safe_str_list(pay.get("payment_references", [])),
        "shipment_ids": _safe_str_list(ship.get("shipment_ids", [])),
    }

    recommended_refund = _bounded_float(
        pay.get("recommended_refund_brl"), 0.0, 0.0, float("inf")
    )
    refund_lines = _sanitize_refund_lines(pay.get("refund_lines", []))

    valid_party_types = {
        "seller", "platform", "logistics_provider",
        "payment_provider", "customer", "unknown",
    }
    responsible_parties = []
    for p in _dict_items(pa.get("responsible_parties"), 5):
        pt = p.get("party_type", "unknown")
        if pt not in valid_party_types:
            pt = "unknown"
        party_id = p.get("party_id")
        responsible_parties.append({
            "party_type": pt,
            "party_id": str(party_id)[:128] if party_id is not None else None,
        })

    ranked_causes = []
    for cause in _dict_items(pa.get("ranked_causes"), 5):
        cause_code = str(cause.get("cause_code", "INSUFFICIENT_EVIDENCE"))[:80]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", cause_code):
            cause_code = "INSUFFICIENT_EVIDENCE"
        ranked_causes.append({
            "cause_code": cause_code,
            "rank": max(1, min(5, _safe_int(cause.get("rank"), 5))),
        })

    valid_ref_set = set(unique_refs)
    valid_verdicts = {"supported", "unsupported", "partially_supported", "insufficient_evidence"}
    claim_assessments = []
    for c in _dict_items(pa.get("claim_assessments"), 5):
        claim_id = str(c.get("claim_id", ""))[:64]
        if not claim_id:
            continue
        verdict = c.get("verdict", "insufficient_evidence")
        if verdict not in valid_verdicts:
            verdict = "insufficient_evidence"
        claim_ev = _unique_strings(
            [r for r in _list(c.get("evidence_refs")) if r in valid_ref_set], 30, 99
        )
        claim_assessments.append({
            "claim_id": claim_id,
            "verdict": verdict,
            "confidence": _bounded_float(c.get("confidence"), 0.5, 0.0, 1.0),
            "evidence_refs": claim_ev,
        })

    data_conflicts = []
    for dc in _dict_items(pa.get("data_conflicts"), 5):
        sources = _unique_strings(dc.get("sources"), 5, 80)
        if len(sources) < 2:
            continue
        selected_source = dc.get("selected_source")
        data_conflicts.append({
            "field": str(dc.get("field", "unknown"))[:100],
            "sources": sources,
            "selected_source": (
                str(selected_source)[:80] if selected_source is not None else None
            ),
            "resolution_code": str(dc.get("resolution_code") or "MANUAL_REVIEW")[:80],
        })

    resolution_actions = _unique_strings(pa.get("resolution_actions"), 8, 80)
    valid_issues = {
        "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
        "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
        "duplicate_charge", "refund_pending", "refund_failed", "unsupported_claim",
        "insufficient_evidence",
    }
    primary_issue = pa.get("primary_issue", "insufficient_evidence")
    if primary_issue not in valid_issues:
        primary_issue = "insufficient_evidence"
    valid_statuses = {"action_required", "no_action", "needs_investigation"}
    case_status = pa.get("case_status", "needs_investigation")
    if case_status not in valid_statuses:
        case_status = "needs_investigation"

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": _bounded_float(pa.get("confidence"), 0.5, 0.0, 1.0),
        },
        "affected_entities": affected_entities,
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": unique_refs[:30],
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": resolution_actions,
    }

    if claim_assessments:
        output["claim_assessments"] = claim_assessments

    return output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_str_list(val: Any, max_items: int = 20) -> list[str]:
    return _unique_strings(val, max_items, 128)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict_items(value: Any, limit: int) -> list[dict[str, Any]]:
    return [item for item in _list(value) if isinstance(item, dict)][:limit]


def _unique_strings(value: Any, limit: int, max_length: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _list(value):
        text = str(item)[:max_length] if item is not None else ""
        if text and text not in seen:
            seen.add(text)
            result.append(text)
        if len(result) == limit:
            break
    return result


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(minimum, min(maximum, number))


def _sanitize_refund_lines(raw: Any) -> list[dict[str, Any]]:
    result = []
    for item in _dict_items(raw, 10):
        entity_id = item.get("entity_id")
        result.append({
            "reason_code": str(item.get("reason_code") or "REFUND")[:80],
            "amount_brl": _bounded_float(item.get("amount_brl"), 0.0, 0.0, float("inf")),
            "entity_id": str(entity_id)[:128] if entity_id is not None else None,
        })
    return result


# ---------------------------------------------------------------------------
# Coordinator / Entry point
# ---------------------------------------------------------------------------
async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Coordinator: dispatches specialist agents then Verifier."""
    case_id = case["case_id"]
    client = _get_client()

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        attributes={"model": _MODEL, "policy_version": case.get("policy_version", "unknown")},
    )

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="order-agent")
    order_result = await _order_item_agent(case, gateway, trace, client)

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="payment-agent")
    payment_result = await _payment_agent(case, gateway, trace, client, order_result)

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="shipment-agent")
    shipment_result = await _shipment_agent(case, gateway, trace, client)

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="policy-agent")
    policy_result = await _policy_agent(
        case, gateway, trace, client,
        order_result, payment_result, shipment_result,
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=policy_result["analysis"].get(
            "primary_issue", "insufficient_evidence"
        ),
        evidence_refs=policy_result["evidence_refs"],
    )

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="verifier-agent")
    output = _verifier_agent(case, order_result, payment_result, shipment_result, policy_result)
    gateway.validate_output(output)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="coordinator",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"][:10],
    )

    return output
