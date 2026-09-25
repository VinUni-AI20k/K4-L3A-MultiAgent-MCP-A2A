"""Coordinator → domain specialists → policy → independent verifier."""

from __future__ import annotations

from typing import Any

from .analysis import (
    ZERO,
    analyze_payment,
    analyze_shipment,
    choose_issue,
    invoice_total,
    money,
    obj,
    rows,
    timestamp,
)
from .evidence import CaseEvidence, Finding
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verifier import evidence_entities, verify_output

PAYMENT_TOOLS = ["get_order_payments", "get_payment_timeline"]


async def order_agent(ledger: CaseEvidence) -> Finding:
    actor = "order-item-agent"
    ledger.assign(actor, "VERIFY_ORDER_SCOPE")
    order = await ledger.fetch(actor, "get_order", required=True, order_id=ledger.order_id)
    if order is not None and not isinstance(order, dict):
        raise ValueError("get_order: expected an object or null")
    exists = obj(order).get("order_id") == ledger.order_id
    if exists:
        await ledger.fetch(actor, "get_order_items", order_id=ledger.order_id)
    finding = Finding(
        actor, {"order": obj(order), "exists": exists}, ["get_order", "get_order_items"]
    )
    ledger.handoff(finding)
    return finding


async def payment_agent(ledger: CaseEvidence, case: dict[str, Any]) -> Finding:
    actor = "payment-agent"
    ledger.assign(actor, "RECONCILE_PAYMENT_LIFECYCLE")
    for tool in PAYMENT_TOOLS:
        await ledger.fetch(actor, tool, order_id=ledger.order_id)
    topic_set = {
        claim.get("topic") for claim in rows(obj(case.get("customer_request")).get("claims"))
    }
    # Claims only route extra investigation. They never determine the verdict.
    events = rows(obj(ledger.data("get_payment_timeline")).get("events"))
    investigate_refund = bool(topic_set & {"refund_pending", "refund_failed"}) or any(
        "refund" in str(event.get("event_type", "")) for event in events
    )
    if investigate_refund:
        await ledger.fetch(actor, "get_refund_timeline", order_id=ledger.order_id)
    facts = analyze_payment(
        ledger.data("get_order_payments"),
        ledger.data("get_payment_timeline"),
        ledger.data("get_refund_timeline"),
        timestamp(case["opened_at"]),
        timestamp(obj(ledger.data("get_order")).get("order_purchase_timestamp")),
        invoice_total(
            ledger.data("get_order_items"),
            timestamp(obj(ledger.data("get_order")).get("order_purchase_timestamp")),
            timestamp(case["opened_at"]),
        ),
    )
    facts["refund_unavailable"] = investigate_refund and "get_refund_timeline" in ledger.failures
    finding = Finding(actor, facts, [*PAYMENT_TOOLS, "get_refund_timeline"])
    ledger.handoff(finding)
    return finding


async def shipment_agent(ledger: CaseEvidence, case: dict[str, Any], status: str) -> Finding:
    actor = "shipment-agent"
    ledger.assign(actor, "VERIFY_DELIVERY_TIMELINE")
    data = await ledger.fetch(actor, "get_shipment_summary", order_id=ledger.order_id)
    finding = Finding(
        actor,
        analyze_shipment(
            data,
            timestamp(case["opened_at"]),
            status,
            timestamp(obj(ledger.data("get_order")).get("order_purchase_timestamp")),
        ),
        ["get_shipment_summary", "get_order_items"],
    )
    ledger.handoff(finding)
    return finding


def _conflicts(
    ledger: CaseEvidence, payment: dict[str, Any], shipment_facts: dict[str, Any]
) -> list[dict[str, Any]]:
    conflicts = []
    order, shipment = obj(ledger.data("get_order")), obj(ledger.data("get_shipment_summary"))
    for left, right in (
        ("order_status", "order_status"),
        ("order_delivered_customer_date", "delivered_customer_at"),
        ("order_delivered_carrier_date", "delivered_carrier_at"),
        ("order_estimated_delivery_date", "estimated_delivery_at"),
    ):
        if left in order and right in shipment and order[left] != shipment[right]:
            conflicts.append(
                {
                    "field": right,
                    "sources": ["get_order", "get_shipment_summary"],
                    "selected_source": None,
                    "resolution_code": "UNRESOLVED_SOURCE_CONFLICT",
                }
            )
    if payment.get("ambiguous_base"):
        conflicts.append(
            {
                "field": "payment_sequential",
                "sources": ["get_order_payments", "get_payment_timeline"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "USE_CONFIRMED_LIFECYCLE_AS_OF_CASE",
            }
        )
    for _code in shipment_facts.get("resolved", []):
        conflicts.append(
            {
                "field": "shipment_delay_attribution",
                "sources": ["shipment_timestamps", "shipment_events"],
                "selected_source": "shipment_timestamps",
                "resolution_code": "USE_COMPLETE_DELIVERY_TIMESTAMPS",
            }
        )
    for code in shipment_facts.get("unresolved", []):
        conflicts.append(
            {
                "field": "shipment_delay_attribution",
                "sources": ["shipment_timestamps", "shipment_events"],
                "selected_source": None,
                "resolution_code": code,
            }
        )
    return conflicts[:5]


def _evidence_tools(issue: str, ledger: CaseEvidence) -> list[str]:
    tools = ["get_order", *PAYMENT_TOOLS, "get_policy"]
    if issue in {"duplicate_charge", "valid_split_payment"}:
        tools += ["get_order_items"]
    if issue == "late_delivery_seller":
        tools += ["get_order_items", "get_shipment_summary", "get_sellers"]
    elif issue == "late_delivery_logistics":
        tools += ["get_order_items", "get_shipment_summary"]
    elif issue == "unavailable_order_paid":
        tools += ["get_order_items", "get_sellers"]
    elif issue == "unsupported_claim":
        tools += ["get_shipment_summary"]
    elif issue == "insufficient_evidence":
        tools = list(ledger.records)
    if issue.startswith("refund_"):
        tools += ["get_refund_timeline"]
    return list(dict.fromkeys(tool for tool in tools if tool in ledger.records))


def _entities(tools: list[str], ledger: CaseEvidence) -> dict[str, list[str]]:
    found = {
        name: set()
        for name in ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
    }
    for name in tools:
        if name != "get_policy":
            for field, values in evidence_entities(ledger.data(name)).items():
                found[field].update(values)
    return {field: sorted(values) for field, values in found.items()}


def _parties(
    rule: dict[str, Any], entities: dict[str, list[str]], late_sellers: list[str]
) -> list[dict[str, Any]]:
    parties = []
    for party in rows(rule.get("responsible_parties")):
        party = dict(party)
        if party.get("party_type") == "seller":
            candidates = late_sellers or entities["seller_ids"]
            if party.get("party_id") in candidates:
                candidates = [party["party_id"]]
            # Never blame all sellers merely because they share the order.
            if len(candidates) == 1 or late_sellers:
                parties.extend(
                    {"party_type": "seller", "party_id": seller} for seller in candidates
                )
            else:
                parties.append({"party_type": "unknown", "party_id": None})
        else:
            parties.append(party)
    return [party for index, party in enumerate(parties) if party not in parties[:index]]


def _claim_results(
    case: dict[str, Any],
    issue: str,
    payment: dict[str, Any],
    shipment: dict[str, Any],
    refund: Any,
    confidence: float,
    ledger: CaseEvidence,
    used_tools: list[str],
) -> list[dict[str, Any]]:
    results = []
    positive = {issue}
    if payment.get("duplicate"):
        positive.add("duplicate_charge")
    if payment.get("refund_issue"):
        positive.add(payment["refund_issue"])
    if shipment.get("issue"):
        positive.add(shipment["issue"])
    for claim in rows(obj(case.get("customer_request")).get("claims")):
        topic = claim.get("topic")
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == "requested_full_refund":
            remaining = payment.get("remaining")
            verdict = (
                "unsupported"
                if refund == ZERO
                else (
                    "supported"
                    if remaining is not None and refund == remaining
                    else "partially_supported"
                )
            )
        elif topic in positive:
            verdict = "supported"
        elif topic in {"refund_pending", "refund_failed"} and payment.get("refund_unavailable"):
            verdict = "insufficient_evidence"
        else:
            verdict = "unsupported"
        claim_tools = used_tools
        if str(topic).startswith("late_delivery"):
            claim_tools = [
                "get_order",
                "get_order_items",
                "get_shipment_summary",
                "get_sellers",
                "get_policy",
            ]
        results.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": min(confidence, 0.45)
                if verdict == "insufficient_evidence"
                else confidence,
                # Keep causal shipment evidence precise for a delivery claim;
                # financial/refund claims retain the complete selected chain.
                "evidence_refs": ledger.refs(
                    [tool for tool in claim_tools if tool in used_tools]
                ),
            }
        )
    return results


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    request = obj(case.get("customer_request"))
    if (
        not isinstance(request.get("claimed_order_id"), str)
        or timestamp(case.get("opened_at")) is None
    ):
        raise ValueError("Case needs claimed_order_id and a timezone-aware opened_at")
    ledger = CaseEvidence(
        case["case_id"],
        request["claimed_order_id"],
        gateway,
        trace,
        set(await gateway.list_tools()),
    )
    order_finding = await order_agent(ledger)
    order = order_finding.facts["order"]
    payment: dict[str, Any] = {}
    shipment: dict[str, Any] = {"late_sellers": [], "resolved": [], "unresolved": []}
    issue = "insufficient_evidence"
    if order_finding.facts["exists"]:
        payment = (await payment_agent(ledger, case)).facts
        shipment = (await shipment_agent(ledger, case, order.get("order_status", ""))).facts
        issue = choose_issue(order, payment, shipment)
        if payment["refund_unavailable"] and not payment["has_refund_events"]:
            issue = "insufficient_evidence"
    ledger.assign("policy-agent", "APPLY_PUBLIC_POLICY")
    policy = await ledger.fetch(
        "policy-agent", "get_policy", required=True, policy_version=case["policy_version"]
    )
    if obj(policy).get("policy_version") != case["policy_version"]:
        raise ValueError("MCP policy version mismatch")
    rule = obj(obj(obj(policy).get("rules")).get(issue))
    if not rule:
        issue = "insufficient_evidence"
    if issue in {"late_delivery_seller", "unavailable_order_paid"}:
        await ledger.fetch("order-item-agent", "get_sellers", order_id=ledger.order_id)
    conflicts = _conflicts(ledger, payment, shipment)
    if any(conflict["selected_source"] is None for conflict in conflicts):
        issue = "insufficient_evidence"
    refund = money(rule.get("refund_brl"))
    if issue != "insufficient_evidence" and (
        refund is None
        or rule.get("case_status") not in {"action_required", "needs_investigation", "no_action"}
        or not isinstance(rule.get("recommended_action"), str)
    ):
        issue = "insufficient_evidence"
    if issue != "insufficient_evidence" and refund and refund > ZERO:
        remaining = payment.get("remaining")
        if remaining is None or not payment.get("refund_complete", False):
            issue = "insufficient_evidence"
        elif refund > remaining:
            # Policy grants cannot create money that was never captured.
            refund = remaining
    tools = _evidence_tools(issue, ledger)
    # Conflict descriptions also need their source evidence in the output.
    for conflict in conflicts:
        tools += [
            name for name in conflict["sources"] if name in ledger.records and name not in tools
        ]
    entities = _entities(tools, ledger)
    if issue == "insufficient_evidence":
        refund, status, action = ZERO, "needs_investigation", "request_additional_evidence"
        confidence = 0.45
        parties = [{"party_type": "unknown", "party_id": None}]
    else:
        status, action = rule["case_status"], rule["recommended_action"]
        parties = _parties(rule, entities, shipment["late_sellers"])
        confidence = 0.96
        if conflicts or payment.get("ambiguous_base"):
            confidence = 0.90
        elif issue == "unsupported_claim":
            confidence = 0.95
        if any(ledger.records[tool].get("warnings") for tool in tools):
            confidence = min(confidence, 0.78)
        if payment.get("inferred_duplicate"):
            confidence = min(confidence, 0.94)
    ledger.trace.emit(
        case_id=ledger.case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=issue.upper(),
        evidence_refs=ledger.refs(["get_policy"]),
        attributes={"refund_brl": float(refund), "status": status},
    )
    ledger.handoff(Finding("policy-agent", {"issue": issue, "status": status}, tools))
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": ledger.case_id,
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": entities,
        "claim_assessments": _claim_results(
            case, issue, payment, shipment, refund, confidence, ledger, tools
        ),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": ledger.refs(tools),
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": [
                {"reason_code": action, "amount_brl": float(refund), "entity_id": ledger.order_id}
            ]
            if refund > ZERO
            else [],
        },
        "resolution_actions": [action],
    }
    ledger.assign("verifier", "VERIFY_CONTRACT_AND_EVIDENCE")
    ledger.handoff(Finding("coordinator", {}, tools), "verifier")
    verify_output(output, ledger)
    trace.emit(
        case_id=ledger.case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="INVARIANTS_PASSED",
        evidence_refs=output["evidence_refs"],
    )
    return output
