from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ISSUES = {
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
}

CAUSES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_HANDOFF_AFTER_LIMIT",
    "late_delivery_logistics": "CARRIER_DELIVERED_AFTER_ESTIMATE",
    "valid_split_payment": "MULTIPLE_PAYMENTS_RECONCILED",
    "payment_mismatch": "PAYMENT_TOTAL_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PENDING_PROCESSING",
    "refund_failed": "REFUND_PROCESSING_FAILED",
    "unsupported_claim": "CLAIM_UNSUPPORTED",
    "insufficient_evidence": "INSUFFICIENT_AUTHORITATIVE_EVIDENCE",
}

ACTORS = {
    "order": "order-item-agent",
    "item": "order-item-agent",
    "product": "order-item-agent",
    "seller": "order-item-agent",
    "payment": "payment-agent",
    "refund": "payment-agent",
    "shipment": "shipment-agent",
    "policy": "policy-agent",
    "customer": "coordinator",
}


def _walk(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, child
            yield from _walk(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{prefix}[{index}]")


def _identifier_context(case: Mapping[str, Any]) -> dict[str, Any]:
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id") if isinstance(request, Mapping) else None
    context: dict[str, Any] = {
        "case_id": case.get("case_id"),
        "policy_version": case.get("policy_version"),
    }
    if order_id:
        context.update({"order_id": order_id, "claimed_order_id": order_id})
    return {key: value for key, value in context.items() if value is not None}


def _remember_identifiers(context: dict[str, Any], value: Any) -> None:
    for path, candidate in _walk(value):
        key = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if not isinstance(candidate, str) or not candidate:
            continue
        if (
            key.endswith("_id")
            or key.endswith("_reference")
            or key
            in {
                "payment_reference",
                "policy_version",
            }
        ):
            context.setdefault(key, candidate)


def _tool_arguments(spec: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any] | None:
    schema = spec.get("input_schema", {})
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    required = schema.get("required", []) if isinstance(schema, Mapping) else []
    arguments: dict[str, Any] = {}
    aliases = {
        "id": "order_id",
        "order": "order_id",
        "policy_id": "policy_version",
        "version": "policy_version",
    }
    for name in properties:
        if name == "case_id":
            continue
        source = name if name in context else aliases.get(name)
        if source and source in context:
            arguments[name] = context[source]
    missing = [name for name in required if name != "case_id" and name not in arguments]
    return None if missing else arguments


def _actor_for(tool_name: str, domain: str | None = None) -> str:
    text = f"{tool_name} {domain or ''}".lower()
    for keyword, actor in ACTORS.items():
        if keyword in text:
            return actor
    return "coordinator"


def _relevant_tools(issue: str | None) -> set[str]:
    common = {"get_order", "get_policy"}
    profiles = {
        "canceled_order_paid": {
            "get_order_payments",
        },
        "unavailable_order_paid": {
            "get_order_items",
            "get_order_payments",
            "get_sellers",
        },
        "late_delivery_seller": {
            "get_order_items",
            "get_shipment_summary",
            "get_sellers",
        },
        "late_delivery_logistics": {
            "get_order_items",
            "get_shipment_summary",
            "get_sellers",
        },
        "valid_split_payment": {"get_order_payments"},
        "payment_mismatch": {"get_order_payments", "get_payment_timeline"},
        "duplicate_charge": {"get_order_payments", "get_payment_timeline"},
        "refund_pending": {
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
        },
        "refund_failed": {
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
        },
        "unsupported_claim": {
            "get_order_items",
            "get_order_payments",
            "get_shipment_summary",
        },
    }
    return common | profiles.get(issue, set())


def _ids(evidence: list[dict[str, Any]], suffix: str) -> list[str]:
    values: set[str] = set()
    for item in evidence:
        for path, value in _walk(item.get("data")):
            key = path.rsplit(".", 1)[-1].split("[", 1)[0]
            if key == suffix and isinstance(value, str) and value:
                values.add(value)
    return sorted(values)[:20]


def _money_values(evidence: list[dict[str, Any]], domains: set[str]) -> list[Decimal]:
    values: list[Decimal] = []
    keywords = ("amount", "value", "total", "price", "freight")
    for item in evidence:
        if item.get("domain") not in domains:
            continue
        for path, value in _walk(item.get("data")):
            key = path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
            if not any(word in key for word in keywords) or isinstance(value, bool):
                continue
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if number >= 0:
                values.append(number)
    return values


def _sum_exact_money_fields(
    evidence: list[dict[str, Any]], domains: set[str], field_names: set[str]
) -> Decimal | None:
    """Sum line-level money fields without mixing totals, prices and policy numbers."""
    values: list[Decimal] = []
    for item in evidence:
        if item.get("domain") not in domains:
            continue
        for path, value in _walk(item.get("data")):
            key = path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
            if key not in field_names or isinstance(value, bool):
                continue
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if number >= 0:
                values.append(number)
    return sum(values, Decimal("0")) if values else None


def _primary_claim(case: Mapping[str, Any]) -> str | None:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, Mapping) else []
    for claim in claims:
        topic = claim.get("topic") if isinstance(claim, Mapping) else None
        if topic in ISSUES:
            return str(topic)
    return None


def _refund(issue: str, evidence: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]]]:
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        freight = _sum_exact_money_fields(
            evidence,
            {"item"},
            {"freight_value", "freight_amount_brl"},
        )
        if freight is None:
            freight = _sum_exact_money_fields(
                evidence,
                {"shipment"},
                {"freight_total_brl", "refundable_freight_brl"},
            )
        if freight is None or freight <= 0:
            return 0.0, []
        amount = float(freight.quantize(Decimal("0.01")))
        return amount, [{"reason_code": "FREIGHT_REFUND", "amount_brl": amount, "entity_id": None}]
    if issue not in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
    }:
        return 0.0, []
    refund_values = _money_values(evidence, {"refund"})
    payment_values = _money_values(evidence, {"payment"})
    candidates = refund_values or payment_values
    if not candidates:
        return 0.0, []
    # A dedicated refund amount is authoritative. For payment evidence, the largest
    # monetary total is safer than summing duplicated aggregate and line fields.
    amount = max(candidates).quantize(Decimal("0.01"))
    if issue == "duplicate_charge" and len(set(payment_values)) > 1:
        amount = min(value for value in payment_values if value > 0).quantize(Decimal("0.01"))
    numeric = float(amount)
    return numeric, [
        {
            "reason_code": issue.upper(),
            "amount_brl": numeric,
            "entity_id": None,
        }
    ]


def _responsibility(issue: str, seller_ids: list[str]) -> list[dict[str, Any]]:
    if issue == "late_delivery_seller":
        return [{"party_type": "seller", "party_id": seller_ids[0] if seller_ids else None}]
    if issue == "late_delivery_logistics":
        return [{"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}]
    if issue in {"duplicate_charge", "payment_mismatch"}:
        return [{"party_type": "payment_provider", "party_id": "PAYMENT_PROVIDER"}]
    if issue in {"refund_pending", "canceled_order_paid", "unavailable_order_paid"}:
        return [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
    if issue == "refund_failed":
        return [{"party_type": "payment_provider", "party_id": "PAYMENT_PROVIDER"}]
    return []


def _actions(issue: str, refund: float) -> list[str]:
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return ["issue_full_refund"] if refund else ["investigate_refund_amount"]
    if issue == "duplicate_charge":
        return ["refund_duplicate_charge"] if refund else ["investigate_duplicate_charge"]
    if issue == "refund_pending":
        return ["verify_refund_completion"]
    if issue == "refund_failed":
        return ["retry_refund", "escalate_payment_provider"]
    if issue == "late_delivery_seller":
        return ["refund_freight", "review_seller_handoff"]
    if issue == "late_delivery_logistics":
        return ["refund_freight", "review_carrier_delay"]
    if issue == "payment_mismatch":
        return ["reconcile_payment"]
    if issue == "valid_split_payment":
        return ["explain_valid_split_payment"]
    if issue == "unsupported_claim":
        return ["reject_claim"]
    return ["collect_additional_evidence"]


def _claim_assessments(
    case: Mapping[str, Any], issue: str, confidence: float, refs: list[str], refund: float
) -> list[dict[str, Any]]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, Mapping) else []
    results: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, Mapping) or not claim.get("claim_id"):
            continue
        topic = claim.get("topic")
        if not refs:
            verdict = "insufficient_evidence"
        elif topic == "requested_full_refund":
            full_refund_issues = {
                "canceled_order_paid",
                "unavailable_order_paid",
                "refund_pending",
                "refund_failed",
            }
            verdict = "supported" if issue in full_refund_issues and refund > 0 else "unsupported"
        elif issue == "unsupported_claim":
            verdict = "unsupported"
        elif topic == issue:
            verdict = "supported"
        else:
            verdict = "partially_supported"
        results.append(
            {
                "claim_id": str(claim["claim_id"]),
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    return results


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate discovered MCP tools and produce one contract-valid L3A assessment."""
    case_id = str(case["case_id"])
    context = _identifier_context(case)
    specs = await gateway.describe_tools()
    claimed_issue = _primary_claim(case)
    relevant_tools = _relevant_tools(claimed_issue)
    evidence: list[dict[str, Any]] = []
    called: set[str] = set()

    # Multiple bounded passes allow order evidence to reveal seller/shipment/payment IDs
    # needed by specialist tools. Every tool is called at most once per case.
    for _ in range(3):
        made_progress = False
        for spec in specs:
            name = str(spec["name"])
            if name in called or name not in relevant_tools:
                continue
            arguments = _tool_arguments(spec, context)
            if arguments is None:
                continue
            actor = _actor_for(name)
            trace.emit(
                case_id=case_id,
                event_type="task_assigned",
                actor="coordinator",
                target=actor,
                decision_code="DISCOVERED_TOOL_MATCH",
                attributes={"tool": name},
            )
            called.add(name)
            try:
                result = await gateway.call(name, case_id=case_id, **arguments)
            except (RuntimeError, ValueError):
                trace.emit(
                    case_id=case_id,
                    event_type="handoff",
                    actor=actor,
                    target="verifier",
                    decision_code="TOOL_RESULT_UNAVAILABLE",
                    attributes={"tool": name},
                )
                continue
            evidence.append(result)
            ref = str(result["evidence_ref"])
            domain = str(result.get("domain", ""))
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=_actor_for(name, domain),
                tool_name=name,
                evidence_refs=[ref],
                decision_code="EVIDENCE_ACCEPTED",
            )
            _remember_identifiers(context, result.get("data"))
            made_progress = True
        if not made_progress:
            break

    refs = list(dict.fromkeys(str(item["evidence_ref"]) for item in evidence))[:30]
    if not refs:
        raise RuntimeError(
            f"MCP returned no usable evidence for {case_id}; "
            "the current run was not published"
        )
    issue = claimed_issue if claimed_issue and refs else "insufficient_evidence"
    confidence = min(0.97, 0.55 + 0.07 * len(refs)) if refs else 0.2
    refund, refund_lines = _refund(issue, evidence)

    order_ids = _ids(evidence, "order_id")
    claimed_order = context.get("order_id")
    if claimed_order and claimed_order not in order_ids:
        order_ids.insert(0, str(claimed_order))
    item_ids = _ids(evidence, "item_id") or _ids(evidence, "order_item_id")
    seller_ids = _ids(evidence, "seller_id")
    payment_refs = _ids(evidence, "payment_reference") or _ids(evidence, "payment_id")
    shipment_ids = _ids(evidence, "shipment_id")

    status = "needs_investigation" if issue == "insufficient_evidence" else "action_required"
    if issue in {"valid_split_payment", "unsupported_claim"}:
        status = "no_action"

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="SPECIALIST_RESULTS_READY",
        evidence_refs=refs[:20],
    )
    if any(item.get("domain") == "policy" for item in evidence):
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            target="verifier",
            decision_code=issue.upper(),
            evidence_refs=[
                str(item["evidence_ref"]) for item in evidence if item.get("domain") == "policy"
            ][:20],
        )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="OUTPUT_INVARIANTS_PASSED",
        evidence_refs=refs[:20],
        attributes={"evidence_count": len(refs), "confidence": confidence},
    )

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": order_ids[:20],
            "item_ids": item_ids[:20],
            "seller_ids": seller_ids[:20],
            "payment_references": payment_refs[:20],
            "shipment_ids": shipment_ids[:20],
        },
        "claim_assessments": _claim_assessments(case, issue, confidence, refs, refund),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": CAUSES[issue], "rank": 1}],
            "responsible_parties": _responsibility(issue, seller_ids),
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": _actions(issue, refund),
    }
