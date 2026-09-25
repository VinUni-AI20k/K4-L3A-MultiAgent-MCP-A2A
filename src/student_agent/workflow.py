from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .mcp_gateway import EvidenceGateway
from .model_client import ALLOWED_MODELS, ModelError, OpenRouterClient
from .trace import TraceWriter

PRIMARY_ISSUES = {
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

ISSUE_TOOLS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_order_payments", "get_policy"),
    "unavailable_order_paid": (
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_policy",
    ),
    "late_delivery_seller": (
        "get_order",
        "get_order_items",
        "get_sellers",
        "get_shipment_summary",
        "get_policy",
    ),
    "late_delivery_logistics": (
        "get_order",
        "get_shipment_summary",
        "get_policy",
    ),
    "valid_split_payment": ("get_order", "get_payment_timeline", "get_policy"),
    "payment_mismatch": ("get_order", "get_payment_timeline", "get_policy"),
    "duplicate_charge": ("get_order", "get_payment_timeline", "get_policy"),
    "refund_pending": ("get_order", "get_refund_timeline", "get_policy"),
    "refund_failed": ("get_order", "get_refund_timeline", "get_policy"),
    "unsupported_claim": (
        "get_order",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    ),
}

TOOL_ACTORS = {
    "get_order": "order-agent",
    "get_order_items": "order-agent",
    "get_sellers": "order-agent",
    "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
    "get_shipment_summary": "shipment-agent",
    "get_policy": "policy-agent",
}


class CaseModel(Protocol):
    model: str

    async def verify_case(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EvidenceRecord:
    tool_name: str
    actor: str
    evidence_ref: str
    domain: str
    data: Any
    warnings: tuple[str, ...]


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _claims(case: dict[str, Any]) -> list[dict[str, Any]]:
    request = case.get("customer_request")
    if not isinstance(request, dict):
        raise ValueError("customer_request must be an object")
    raw_claims = request.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        raise ValueError("customer_request.claims must be a non-empty array")
    claims = [claim for claim in raw_claims if isinstance(claim, dict)]
    if len(claims) != len(raw_claims):
        raise ValueError("every customer claim must be an object")
    return claims


def _requested_issue(claims: list[dict[str, Any]]) -> str:
    topics = [claim.get("topic") for claim in claims]
    candidates = [topic for topic in topics if isinstance(topic, str) and topic in ISSUE_TOOLS]
    if len(set(candidates)) != 1:
        return "insufficient_evidence"
    return candidates[0]


async def _collect_evidence(
    *,
    case_id: str,
    order_id: str,
    policy_version: str,
    issue: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    for tool_name in ISSUE_TOOLS.get(issue, ("get_order", "get_policy")):
        actor = TOOL_ACTORS[tool_name]
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code="DOMAIN_EVIDENCE_REQUESTED",
            attributes={"tool_name": tool_name},
        )
        arguments = (
            {"policy_version": policy_version}
            if tool_name == "get_policy"
            else {"order_id": order_id}
        )
        evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
        record = EvidenceRecord(
            tool_name=tool_name,
            actor=actor,
            evidence_ref=_required_string(evidence.get("evidence_ref"), "evidence_ref"),
            domain=_required_string(evidence.get("domain"), "evidence domain"),
            data=evidence.get("data"),
            warnings=tuple(str(item) for item in evidence.get("warnings", [])),
        )
        records.append(record)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[record.evidence_ref],
            attributes={"domain": record.domain, "warning_count": len(record.warnings)},
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            decision_code="EVIDENCE_VALIDATED",
            evidence_refs=[record.evidence_ref],
        )
        if tool_name == "get_policy":
            trace.emit(
                case_id=case_id,
                event_type="policy_decided",
                actor="policy-agent",
                target="coordinator",
                decision_code="AUTHORITATIVE_POLICY_LOADED",
                evidence_refs=[record.evidence_ref],
            )
    return records


def _record(records: list[EvidenceRecord], tool_name: str) -> EvidenceRecord | None:
    return next((record for record in records if record.tool_name == tool_name), None)


def _objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _events(records: list[EvidenceRecord], tool_name: str) -> list[dict[str, Any]]:
    record = _record(records, tool_name)
    if record is None or not isinstance(record.data, dict):
        return []
    return _objects(record.data.get("events"))


def _payment_rows(records: list[EvidenceRecord]) -> list[dict[str, Any]]:
    timeline = _record(records, "get_payment_timeline")
    if timeline is not None and isinstance(timeline.data, dict):
        return _objects(timeline.data.get("payments"))
    payments = _record(records, "get_order_payments")
    return _objects(payments.data) if payments is not None else []


def _supports_issue(issue: str, records: list[EvidenceRecord]) -> bool:
    order = _record(records, "get_order")
    order_data = order.data if order is not None and isinstance(order.data, dict) else {}
    payments = _payment_rows(records)
    payment_events = _events(records, "get_payment_timeline")
    refund_events = _events(records, "get_refund_timeline")
    shipment_events = _events(records, "get_shipment_summary")

    if issue == "canceled_order_paid":
        return order_data.get("order_status") == "canceled" and bool(payments)
    if issue == "unavailable_order_paid":
        return order_data.get("order_status") == "unavailable" and bool(payments)
    if issue == "late_delivery_seller":
        return any(
            event.get("event_type") == "delivered_late" and event.get("actor") == "seller"
            for event in shipment_events
        )
    if issue == "late_delivery_logistics":
        return any(
            event.get("event_type") == "delivered_late"
            and event.get("actor") == "logistics_provider"
            for event in shipment_events
        )
    if issue == "payment_mismatch":
        return any(event.get("event_type") == "reconciliation_mismatch" for event in payment_events)
    if issue == "duplicate_charge":
        signatures = [
            (
                row.get("payment_sequential"),
                row.get("payment_type"),
                row.get("payment_value"),
            )
            for row in payments
        ]
        return len(signatures) != len(set(signatures))
    if issue == "valid_split_payment":
        payment_types = {row.get("payment_type") for row in payments}
        sequences = {row.get("payment_sequential") for row in payments}
        has_anomaly = any(
            event.get("event_type") == "reconciliation_mismatch" for event in payment_events
        )
        return len(payment_types) >= 2 and len(sequences) >= 2 and not has_anomaly
    if issue in {"refund_pending", "refund_failed"}:
        expected_status = issue.removeprefix("refund_")
        return any(event.get("status") == expected_status for event in refund_events)
    if issue == "unsupported_claim":
        shipment = _record(records, "get_shipment_summary")
        shipment_data = (
            shipment.data if shipment is not None and isinstance(shipment.data, dict) else {}
        )
        delivered_at = _timestamp(shipment_data.get("delivered_customer_at"))
        estimated_at = _timestamp(shipment_data.get("estimated_delivery_at"))
        return (
            order_data.get("order_status") == "delivered"
            and delivered_at is not None
            and estimated_at is not None
            and delivered_at <= estimated_at
        )
    return False


def _policy_rule(issue: str, records: list[EvidenceRecord]) -> dict[str, Any] | None:
    policy = _record(records, "get_policy")
    if policy is None or not isinstance(policy.data, dict):
        return None
    rules = policy.data.get("rules")
    if not isinstance(rules, dict):
        return None
    rule = rules.get(issue)
    return rule if isinstance(rule, dict) else None


def _unique_strings(values: list[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return result


def _entities(order_id: str, records: list[EvidenceRecord]) -> dict[str, list[str]]:
    item_rows: list[dict[str, Any]] = []
    seller_rows: list[dict[str, Any]] = []
    shipment_rows: list[dict[str, Any]] = []
    item_record = _record(records, "get_order_items")
    seller_record = _record(records, "get_sellers")
    shipment_record = _record(records, "get_shipment_summary")
    if item_record is not None:
        item_rows = _objects(item_record.data)
    if seller_record is not None:
        seller_rows = _objects(seller_record.data)
    if shipment_record is not None and isinstance(shipment_record.data, dict):
        shipment_rows = _objects(shipment_record.data.get("shipping_limits"))
    seller_ids = [row.get("seller_id") for row in [*item_rows, *seller_rows, *shipment_rows]]
    return {
        "order_ids": [order_id],
        "item_ids": _unique_strings([row.get("order_item_id") for row in item_rows]),
        "seller_ids": _unique_strings(seller_ids),
        "payment_references": [],
        "shipment_ids": [],
    }


def _money(value: object) -> float:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0.0
    return float(amount.quantize(Decimal("0.01")))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _claim_assessments(
    claims: list[dict[str, Any]], issue: str, evidence_refs: list[str], supported: bool
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for claim in claims[:5]:
        claim_id = claim.get("claim_id")
        topic = claim.get("topic")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        if not supported:
            verdict, confidence = "insufficient_evidence", 0.3
        elif topic == issue:
            verdict, confidence = "supported", 0.96
        elif topic == "requested_full_refund":
            if issue in {"canceled_order_paid", "unavailable_order_paid"}:
                verdict, confidence = "supported", 0.92
            elif issue in {"unsupported_claim", "valid_split_payment"}:
                verdict, confidence = "unsupported", 0.94
            elif issue == "refund_pending":
                verdict, confidence = "insufficient_evidence", 0.7
            else:
                verdict, confidence = "partially_supported", 0.86
        else:
            verdict, confidence = "unsupported", 0.85
        results.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs,
            }
        )
    return results


def _candidate_output(
    *,
    case_id: str,
    order_id: str,
    claims: list[dict[str, Any]],
    issue: str,
    records: list[EvidenceRecord],
) -> dict[str, Any]:
    supported = _supports_issue(issue, records)
    rule = _policy_rule(issue, records) if supported else None
    evidence_refs = _unique_strings([record.evidence_ref for record in records])
    if rule is None:
        issue = "insufficient_evidence"
        status = "needs_investigation"
        confidence = 0.35
        refund = 0.0
        action = "MANUAL_INVESTIGATION"
        parties = [{"party_type": "unknown", "party_id": None}]
    else:
        status = str(rule.get("case_status", "needs_investigation"))
        confidence = 0.94 if status == "action_required" else 0.91
        refund = _money(rule.get("refund_brl", 0))
        action = str(rule.get("recommended_action", "manual_investigation")).upper()
        raw_parties = rule.get("responsible_parties", [])
        parties = _objects(raw_parties) or [{"party_type": "unknown", "party_id": None}]
    refund_lines = []
    if refund > 0:
        party_id = next(
            (party.get("party_id") for party in parties if party.get("party_id")), order_id
        )
        refund_lines.append(
            {"reason_code": action, "amount_brl": refund, "entity_id": party_id}
        )
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": _entities(order_id, records),
        "claim_assessments": _claim_assessments(
            claims, issue, evidence_refs, supported=rule is not None
        ),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }


def _model_payload(
    case: dict[str, Any], output: dict[str, Any], records: list[EvidenceRecord]
) -> dict[str, Any]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    issue = str(output["assessment"]["primary_issue"])
    return {
        "claim_topics": [
            claim.get("topic") for claim in _objects(claims) if isinstance(claim.get("topic"), str)
        ],
        "allowed_primary_issues": sorted(PRIMARY_ISSUES),
        "deterministic_candidate": {
            "assessment": output["assessment"],
            "financial_resolution": output["financial_resolution"],
            "resolution_actions": output["resolution_actions"],
        },
        "evidence": [_model_evidence_summary(record, issue) for record in records],
    }


def _model_evidence_summary(record: EvidenceRecord, issue: str) -> dict[str, Any]:
    """Minimize external model data: omit messages, IDs, refs, and timestamps."""
    data = record.data
    summary: Any = None
    if record.tool_name == "get_order" and isinstance(data, dict):
        summary = {"order_status": data.get("order_status")}
    elif record.tool_name in {"get_order_payments", "get_order_items"}:
        allowed = (
            ("payment_sequential", "payment_type", "payment_value")
            if record.tool_name == "get_order_payments"
            else ("price", "freight_value")
        )
        summary = [{key: row.get(key) for key in allowed} for row in _objects(data)]
    elif record.tool_name in {
        "get_payment_timeline",
        "get_refund_timeline",
        "get_shipment_summary",
    } and isinstance(data, dict):
        delivered_at = _timestamp(data.get("delivered_customer_at"))
        estimated_at = _timestamp(data.get("estimated_delivery_at"))
        summary = {
            "order_status": data.get("order_status"),
            "delivered_on_time": (
                delivered_at <= estimated_at
                if delivered_at is not None and estimated_at is not None
                else None
            ),
            "payments": [
                {
                    key: row.get(key)
                    for key in ("payment_sequential", "payment_type", "payment_value")
                }
                for row in _objects(data.get("payments"))
            ],
            "events": [
                {key: row.get(key) for key in ("event_type", "status", "actor", "amount_brl")}
                for row in _objects(data.get("events"))
            ],
        }
    elif record.tool_name == "get_policy" and isinstance(data, dict):
        rules = data.get("rules")
        rule = rules.get(issue) if isinstance(rules, dict) else None
        if isinstance(rule, dict):
            summary = {
                "case_status": rule.get("case_status"),
                "recommended_action": rule.get("recommended_action"),
                "refund_brl": rule.get("refund_brl"),
                "responsible_party_types": [
                    party.get("party_type") for party in _objects(rule.get("responsible_parties"))
                ],
            }
    elif record.tool_name == "get_sellers":
        summary = {"seller_count": len(_objects(data))}
    return {"tool": record.tool_name, "domain": record.domain, "facts": summary}


async def _model_verification(
    model: CaseModel, payload: dict[str, Any], expected_issue: str
) -> tuple[bool, float | None]:
    response = await model.verify_case(payload)
    issue = response.get("primary_issue")
    confidence = response.get("confidence")
    if issue not in PRIMARY_ISSUES:
        raise ModelError("verifier returned an unsupported primary_issue")
    if not isinstance(confidence, int | float) or isinstance(confidence, bool):
        raise ModelError("verifier confidence must be numeric")
    normalized_confidence = max(0.0, min(1.0, float(confidence)))
    return issue == expected_issue, normalized_confidence


def _verify_output(case_id: str, output: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if output.get("case_id") != case_id:
        errors.append("CASE_ID_MISMATCH")
    refs = output.get("evidence_refs", [])
    if not isinstance(refs, list) or len(refs) != len(set(refs)):
        errors.append("INVALID_EVIDENCE_REFS")
    actions = output.get("resolution_actions", [])
    if not isinstance(actions, list) or len(actions) != len(set(actions)):
        errors.append("DUPLICATE_ACTIONS")
    financial = output.get("financial_resolution", {})
    if not isinstance(financial, dict):
        errors.append("INVALID_FINANCIAL_RESOLUTION")
    else:
        refund = _money(financial.get("recommended_refund_brl", 0))
        lines = _objects(financial.get("refund_lines"))
        line_total = round(sum(_money(line.get("amount_brl", 0)) for line in lines), 2)
        if line_total != round(refund, 2):
            errors.append("REFUND_TOTAL_MISMATCH")
    return errors


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    model: CaseModel | None = None,
) -> dict[str, Any]:
    """Coordinate scoped specialists, a sub-10B verifier, and deterministic safeguards."""
    case_id = _required_string(case.get("case_id"), "case_id")
    request = case.get("customer_request")
    if not isinstance(request, dict):
        raise ValueError("customer_request must be an object")
    order_id = _required_string(request.get("claimed_order_id"), "claimed_order_id")
    policy_version = _required_string(case.get("policy_version"), "policy_version")
    claims = _claims(case)
    requested_issue = _requested_issue(claims)

    records = await _collect_evidence(
        case_id=case_id,
        order_id=order_id,
        policy_version=policy_version,
        issue=requested_issue,
        gateway=gateway,
        trace=trace,
    )
    output = _candidate_output(
        case_id=case_id,
        order_id=order_id,
        claims=claims,
        issue=requested_issue,
        records=records,
    )

    verifier = model or OpenRouterClient.from_env()
    if verifier.model not in ALLOWED_MODELS:
        raise ValueError("verifier model is not in the approved sub-10B allowlist")
    model_agreed = False
    model_confidence: float | None = None
    decision_code = "MODEL_UNAVAILABLE"
    try:
        model_agreed, model_confidence = await _model_verification(
            verifier,
            _model_payload(case, output, records),
            str(output["assessment"]["primary_issue"]),
        )
        decision_code = "MODEL_AGREED" if model_agreed else "MODEL_DISAGREED"
    except ModelError:
        decision_code = "MODEL_UNAVAILABLE"

    if not model_agreed:
        output["assessment"]["confidence"] = min(
            float(output["assessment"]["confidence"]), 0.75
        )
    elif model_confidence is not None:
        output["assessment"]["confidence"] = round(
            min(float(output["assessment"]["confidence"]), model_confidence), 2
        )

    errors = _verify_output(case_id, output)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=f"verifier-{verifier.model}",
        target="coordinator",
        decision_code="VERIFICATION_FAILED" if errors else decision_code,
        evidence_refs=output["evidence_refs"],
        attributes={
            "error_count": len(errors),
            "model_agreed": model_agreed,
            "model_parameters_billion": ALLOWED_MODELS[verifier.model],
        },
    )
    if errors:
        raise ValueError("output verification failed: " + ", ".join(errors))
    return output
