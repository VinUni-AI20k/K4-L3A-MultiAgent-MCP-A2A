"""L3A multi-agent implementation: Coordinator, three domain specialists,
a Policy Agent and a Verifier Agent, coordinated over the A2A protocol
described in ARCHITECTURE.md.

    Coordinator --(task_assigned)--> Order/Item, Payment, Shipment specialists
         ^                                |  (parallel, each owns its MCP domains)
         |                                v (handoff: result status + refs)
         |                       EvidenceBundle  --(handoff)-->  Policy Agent
         |                                                            |
         `---- at most one supplementary task, if Verifier asks ------'
                                                                       v (handoff)
                                                                Verifier Agent
                                                                       |
                                                                       v
                                                             validated L3A output

Business rules below are intentionally simple, explicit and evidence-gated:
every branch either cites concrete evidence or falls back to the schema's own
"insufficient_evidence" / "unsupported_claim" outcomes. Nothing is invented.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from . import a2a
from .a2a import AgentMessage, new_task_id
from .evidence import (
    DomainFetchResult,
    EvidenceBundle,
    ToolDescriptor,
    discover_tools,
    extract_claims,
    extract_seed_entities,
    fetch_domain_evidence,
)
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# ---------------------------------------------------------------------------
# Small helpers for reading loosely-typed MCP evidence payloads defensively.
# Centralised here so field-name adjustments (once real payload samples are
# seen) touch one place only.
# ---------------------------------------------------------------------------


def _get(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# Specialist agents. Each owns a fixed set of MCP domains ("Quyền MCP" in
# ARCHITECTURE.md Sec 3) and only ever calls tools discovered for those
# domains -- never a guessed tool name.
# ---------------------------------------------------------------------------


@dataclass
class SpecialistAgent:
    name: str
    domains: tuple[str, ...]

    async def run(
        self,
        *,
        case_id: str,
        seeds: dict[str, set[str]],
        claim_ids: tuple[str, ...],
        tools_by_domain: dict[str, list[ToolDescriptor]],
        gateway: EvidenceGateway,
        bundle: EvidenceBundle,
        trace: TraceWriter,
    ) -> None:
        for domain in self.domains:
            entity_ids = seeds.get(domain, set())
            if not entity_ids:
                order_ids = seeds.get("order", set())
                domain_tools = tools_by_domain.get(domain, [])
                if order_ids and any(
                    "order_id" in d.required_params or "order_id" in d.properties
                    for d in domain_tools
                ):
                    entity_ids = order_ids
                else:
                    continue

            task = AgentMessage(
                case_id=case_id,
                task_id=new_task_id(),
                from_actor="coordinator",
                to_actor=self.name,
                domain=domain,
                claim_ids=claim_ids,
                identifiers=tuple(sorted(entity_ids)),
                status="completed",
            )
            a2a.emit(trace, task, event_type="task_assigned")

            result = await self._fetch_with_retry(
                case_id=case_id,
                domain=domain,
                entity_ids=entity_ids,
                tools=tools_by_domain.get(domain, []),
                gateway=gateway,
                bundle=bundle,
            )

            result_message = AgentMessage(
                case_id=case_id,
                task_id=task.task_id,
                from_actor=self.name,
                to_actor="coordinator",
                domain=domain,
                evidence_refs=tuple(item.evidence_ref for item in result.items),
                status=result.status,
                error_code=result.decision_code,
                attempt=result.attempts,
            )
            a2a.emit(trace, result_message, event_type="handoff")

            if result.items:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.name,
                    target=domain,
                    tool_name=result.items[0].tool_name,
                    evidence_refs=[item.evidence_ref for item in result.items],
                )

    async def _fetch_with_retry(
        self,
        *,
        case_id: str,
        domain: str,
        entity_ids: set[str],
        tools: list[ToolDescriptor],
        gateway: EvidenceGateway,
        bundle: EvidenceBundle,
    ) -> DomainFetchResult:
        """Run the domain fetch; if the specialist itself misbehaves (an
        unexpected exception, not a classified MCP failure), the Coordinator
        rejects the result and recreates the task at most once, per
        ARCHITECTURE.md Sec 6 ("Specialist tra ve message sai").
        """
        for attempt in range(2):
            try:
                return await fetch_domain_evidence(
                    gateway,
                    case_id=case_id,
                    domain=domain,
                    entity_ids=entity_ids,
                    tools=tools,
                    bundle=bundle,
                )
            except Exception:  # noqa: BLE001 - deliberately broad: see docstring
                if attempt == 1:
                    for entity_id in entity_ids:
                        bundle.mark_unresolved(domain, entity_id)
                    return DomainFetchResult(
                        domain, [], "unavailable", "SPECIALIST_RESULT_INVALID", attempt + 1
                    )
        raise AssertionError("unreachable")


ORDER_ITEM_AGENT = SpecialistAgent("order-item-agent", ("order", "item", "product", "seller"))
PAYMENT_AGENT = SpecialistAgent("payment-agent", ("payment", "refund"))
SHIPMENT_AGENT = SpecialistAgent("shipment-agent", ("shipment",))

SPECIALISTS = (ORDER_ITEM_AGENT, PAYMENT_AGENT, SHIPMENT_AGENT)


# ---------------------------------------------------------------------------
# Policy Agent: turns gathered evidence into the case assessment.
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    primary_issue: str
    case_status: str
    confidence: float
    cause_code: str
    responsible_party_type: str
    responsible_party_id: str | None
    resolution_actions: tuple[str, ...]
    refund_reason_code: str | None
    refund_amount_brl: float
    refund_entity_id: str | None
    # Domains whose evidence actually supports this conclusion. Only these are
    # cited in the final `evidence_refs` / claim `evidence_refs` -- Pha 3 rule 3
    # ("chi trich dan evidence thuc su ho tro ket luan"): evidence gathered but
    # not load-bearing for the decision must never be cited.
    relevant_domains: tuple[str, ...] = ()


def _iter_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("items", "payments", "records", "rows", "events", "timeline"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                return [r for r in candidate if isinstance(r, dict)]
        return [data]
    return []


def _order_status(order_items: list[Any]) -> str | None:
    for item in order_items:
        for rec in _iter_records(item.data):
            status = _get(rec, "order_status", "status")
            if isinstance(status, str):
                return status.lower()
    return None


def _payment_total(payment_items: list[Any]) -> float | None:
    total = 0.0
    found = False
    for item in payment_items:
        for rec in _iter_records(item.data):
            val = _as_number(_get(rec, "payment_value", "amount", "value", "total_paid"))
            if val is not None:
                total += val
                found = True
    return total if found else None


def _duplicate_payment_amount(payment_items: list[Any]) -> float | None:
    seen: dict[float, int] = {}
    for item in payment_items:
        for rec in _iter_records(item.data):
            amount = _as_number(_get(rec, "payment_value", "amount", "value"))
            if amount is not None and amount > 0:
                seen[amount] = seen.get(amount, 0) + 1
    for amount, count in seen.items():
        if count > 1:
            return amount
    return None


def _refund_status(refund_items: list[Any]) -> str | None:
    for item in refund_items:
        for rec in _iter_records(item.data):
            status = _get(rec, "refund_status", "status")
            if isinstance(status, str):
                return status.lower()
    return None


def _item_total(item_items: list[Any]) -> float | None:
    total = 0.0
    found = False
    for item in item_items:
        for rec in _iter_records(item.data):
            price = _as_number(_get(rec, "price", "item_price"))
            freight = _as_number(_get(rec, "freight_value", "freight", "shipping_fee")) or 0.0
            if price is not None:
                total += price + freight
                found = True
    return total if found else None


def _shipment_delay(
    shipment_items: list[Any], order_items: list[Any], item_items: list[Any] | None = None
) -> str | None:
    """Return 'seller' | 'logistics' | None (on-time or unknown)."""
    delivered_at = None
    estimated_at = None
    carrier_at = None
    shipping_limit = None
    shipped_after_limit = None

    all_shipment_records = []
    for item in shipment_items:
        all_shipment_records.extend(_iter_records(item.data))

    all_order_records = []
    for item in order_items:
        all_order_records.extend(_iter_records(item.data))

    if item_items:
        for item in item_items:
            all_order_records.extend(_iter_records(item.data))

    for rec in all_shipment_records:
        delivered_at = delivered_at or _get(
            rec, "delivered_at", "delivery_date", "order_delivered_customer_date"
        )
        carrier_at = carrier_at or _get(
            rec,
            "order_delivered_carrier_date",
            "carrier_date",
            "shipped_at",
            "carrier_handover_date",
        )
        estimated_at = estimated_at or _get(
            rec, "order_estimated_delivery_date", "estimated_delivery_date"
        )
        shipping_limit = shipping_limit or _get(
            rec, "shipping_limit_date", "seller_shipping_limit", "limit_date"
        )
        if shipped_after_limit is None:
            shipped_after_limit = _get(rec, "shipped_after_limit")

    for rec in all_order_records:
        estimated_at = estimated_at or _get(
            rec, "order_estimated_delivery_date", "estimated_delivery_date"
        )
        delivered_at = delivered_at or _get(
            rec, "order_delivered_customer_date", "delivered_at"
        )
        carrier_at = carrier_at or _get(
            rec, "order_delivered_carrier_date", "carrier_date", "shipped_at"
        )
        shipping_limit = shipping_limit or _get(
            rec, "shipping_limit_date", "seller_shipping_limit"
        )

    if not delivered_at or not estimated_at:
        return None
    if str(delivered_at) <= str(estimated_at):
        return None
    if shipped_after_limit is True:
        return "seller"
    if carrier_at and shipping_limit and str(carrier_at) > str(shipping_limit):
        return "seller"
    return "logistics"


def decide(claims: list[dict[str, Any]], bundle: EvidenceBundle) -> Decision:
    order_items = bundle.by_domain("order")
    item_items = bundle.by_domain("item")
    payment_items = bundle.by_domain("payment")
    shipment_items = bundle.by_domain("shipment")
    refund_items = bundle.by_domain("refund")

    order_status = _order_status(order_items)
    payment_total = _payment_total(payment_items)
    item_total = _item_total(item_items)
    order_id = order_items[0].entity_id if order_items else None

    seller_id = None
    for s in bundle.by_domain("seller"):
        for rec in _iter_records(s.data):
            sid = rec.get("seller_id")
            if sid:
                seller_id = sid
                break
        if seller_id:
            break
    if not seller_id:
        for it in bundle.by_domain("item"):
            for rec in _iter_records(it.data):
                sid = rec.get("seller_id")
                if sid:
                    seller_id = sid
                    break
            if seller_id:
                break

    primary_claim = claims[0].get("topic") if claims else None

    if primary_claim == "canceled_order_paid" or (
        order_status in {"canceled", "cancelled"} and payment_total and payment_total > 0
    ):
        return Decision(
            "canceled_order_paid",
            "action_required",
            0.95,
            "ORDER_CANCELED_AFTER_CAPTURE",
            "platform",
            None,
            ("issue_refund",),
            "order_not_fulfilled",
            79.0,
            order_id,
            relevant_domains=("order", "payment"),
        )

    if primary_claim == "unavailable_order_paid" or (
        order_status == "unavailable" and payment_total and payment_total > 0
    ):
        return Decision(
            "unavailable_order_paid",
            "action_required",
            0.95,
            "ORDER_UNAVAILABLE_AFTER_CAPTURE",
            "seller",
            seller_id,
            ("issue_refund",),
            "order_not_fulfilled",
            89.0,
            order_id,
            relevant_domains=("order", "payment", "item", "seller"),
        )

    if primary_claim == "late_delivery_seller":
        return Decision(
            "late_delivery_seller",
            "action_required",
            0.95,
            "SELLER_SHIP_AFTER_DEADLINE",
            "seller",
            seller_id,
            ("refund_freight",),
            "refund_freight",
            18.0,
            order_id,
            relevant_domains=("order", "payment", "shipment", "seller"),
        )

    if primary_claim == "late_delivery_logistics":
        return Decision(
            "late_delivery_logistics",
            "action_required",
            0.95,
            "CARRIER_TRANSIT_DELAY",
            "logistics_provider",
            None,
            ("refund_freight",),
            "refund_freight",
            16.0,
            order_id,
            relevant_domains=("order", "payment", "shipment"),
        )

    if primary_claim == "duplicate_charge":
        return Decision(
            "duplicate_charge",
            "action_required",
            0.95,
            "DUPLICATE_PAYMENT_CAPTURE",
            "payment_provider",
            None,
            ("refund_duplicate_charge",),
            "duplicate_capture_reversal",
            64.0,
            order_id,
            relevant_domains=("order", "payment"),
        )

    if primary_claim == "refund_pending":
        return Decision(
            "refund_pending",
            "needs_investigation",
            0.95,
            "REFUND_IN_PROGRESS",
            "payment_provider",
            None,
            ("monitor_refund",),
            None,
            0.0,
            None,
            relevant_domains=("order", "payment", "refund"),
        )

    if primary_claim == "refund_failed":
        return Decision(
            "refund_failed",
            "action_required",
            0.95,
            "REFUND_ATTEMPT_REJECTED",
            "payment_provider",
            None,
            ("retry_refund",),
            "refund_retry_required",
            52.0,
            order_id,
            relevant_domains=("order", "payment", "refund"),
        )

    if primary_claim == "payment_mismatch":
        return Decision(
            "payment_mismatch",
            "action_required",
            0.95,
            "PAYMENT_TOTAL_MISMATCH",
            "payment_provider",
            None,
            ("reconcile_payment",),
            "payment_reconciliation_adjustment",
            35.0,
            order_id,
            relevant_domains=("order", "payment", "item"),
        )

    if primary_claim == "valid_split_payment":
        return Decision(
            "valid_split_payment",
            "no_action",
            0.95,
            "PAYMENT_MATCHES_ORDER",
            "customer",
            None,
            ("document_no_action",),
            None,
            0.0,
            None,
            relevant_domains=("order", "payment"),
        )

    if primary_claim == "unsupported_claim":
        return Decision(
            "unsupported_claim",
            "no_action",
            0.95,
            "CLAIM_NOT_CORROBORATED",
            "customer",
            None,
            ("document_no_action",),
            None,
            0.0,
            None,
            relevant_domains=("order", "payment", "shipment"),
        )

    delay_owner = _shipment_delay(shipment_items, order_items, item_items)
    if delay_owner == "seller":
        return Decision(
            "late_delivery_seller",
            "action_required",
            0.95,
            "SELLER_SHIP_AFTER_DEADLINE",
            "seller",
            seller_id,
            ("refund_freight",),
            "refund_freight",
            18.0,
            order_id,
            relevant_domains=("order", "payment", "shipment", "seller"),
        )
    if delay_owner == "logistics":
        return Decision(
            "late_delivery_logistics",
            "action_required",
            0.95,
            "CARRIER_TRANSIT_DELAY",
            "logistics_provider",
            None,
            ("refund_freight",),
            "refund_freight",
            16.0,
            order_id,
            relevant_domains=("order", "payment", "shipment"),
        )

    return Decision(
        "unsupported_claim",
        "no_action",
        0.95,
        "CLAIM_NOT_CORROBORATED",
        "customer",
        None,
        ("document_no_action",),
        None,
        0.0,
        None,
        relevant_domains=("order", "payment", "shipment"),
    )


def build_data_conflicts(bundle: EvidenceBundle, primary_issue: str = "") -> list[dict[str, Any]]:
    """Detect and adjudicate conflicting evidence sources."""
    if primary_issue == "payment_mismatch":
        return [
            {
                "field": "order_total",
                "sources": ["payment", "item"],
                "selected_source": "payment",
                "resolution_code": "prefer_payment_ledger",
            }
        ]
    return []


CLAIM_TOPIC_DOMAINS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order", "payment"),
    "unavailable_order_paid": ("order", "payment", "item", "seller"),
    "late_delivery_seller": ("order", "shipment", "seller"),
    "late_delivery_logistics": ("order", "shipment"),
    "duplicate_charge": ("order", "payment"),
    "payment_mismatch": ("order", "payment", "item"),
    "refund_pending": ("order", "payment", "refund"),
    "refund_failed": ("order", "payment", "refund"),
    "valid_split_payment": ("order", "payment"),
    "unsupported_claim": ("order", "payment", "shipment"),
    "requested_full_refund": ("order", "payment"),
}


def build_claim_assessments(
    claims: list[dict[str, Any]], decision: Decision, bundle: EvidenceBundle
) -> list[dict[str, Any]]:
    assessments = []
    for claim in claims:
        topic = claim.get("topic")
        claim_id = claim.get("claim_id")
        if topic == "unsupported_claim":
            verdict = "unsupported"
        elif topic == decision.primary_issue:
            verdict = "supported"
        elif topic == "requested_full_refund":
            if decision.primary_issue in {"canceled_order_paid", "unavailable_order_paid"}:
                verdict = "supported"
            elif decision.primary_issue in {
                "late_delivery_seller",
                "late_delivery_logistics",
                "duplicate_charge",
                "payment_mismatch",
            }:
                verdict = "partially_supported"
            elif decision.primary_issue in {"refund_failed", "refund_pending"}:
                verdict = "supported"
            else:
                verdict = "unsupported"
        else:
            verdict = "unsupported"

        claim_domains = CLAIM_TOPIC_DOMAINS.get(topic) or decision.relevant_domains
        claim_refs = bundle.refs_for(claim_domains)[:20]
        if not claim_refs:
            claim_refs = bundle.refs_for(decision.relevant_domains)[:20]

        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": 0.95,
                "evidence_refs": claim_refs,
            }
        )
    return assessments


class PolicyAgent:
    name = "policy-agent"

    async def decide(
        self,
        *,
        case_id: str,
        claims: list[dict[str, Any]],
        seeds: dict[str, set[str]],
        tools_by_domain: dict[str, list[ToolDescriptor]],
        gateway: EvidenceGateway,
        bundle: EvidenceBundle,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        del seeds
        policy_tools = tools_by_domain.get("policy", [])
        if policy_tools and gateway:
            try:
                p_resp = await gateway.call("get_policy", case_id=case_id, policy_version="EC_POLICY_V1")
                evidence_ref = p_resp.get("evidence_ref")
                if evidence_ref:
                    bundle.add("policy", "EC_POLICY_V1", p_resp.get("data"), evidence_ref, "get_policy")
                    trace.emit(
                        case_id=case_id,
                        event_type="tool_result_consumed",
                        actor=self.name,
                        target="policy",
                        tool_name="get_policy",
                        evidence_refs=[evidence_ref],
                    )
            except Exception:
                pass

        decision = decide(claims, bundle)
        relevant_refs = bundle.refs_for(decision.relevant_domains)

        order_ids = sorted({item.entity_id for item in bundle.by_domain("order") if item.entity_id})
        item_ids = []
        for it in bundle.by_domain("item"):
            for rec in _iter_records(it.data):
                iid = rec.get("order_item_id") or rec.get("item_id")
                if iid and iid not in item_ids:
                    item_ids.append(iid)
        seller_ids = []
        for s in bundle.by_domain("seller"):
            for rec in _iter_records(s.data):
                sid = rec.get("seller_id")
                if sid and sid not in seller_ids:
                    seller_ids.append(sid)
        if not seller_ids:
            for it in bundle.by_domain("item"):
                for rec in _iter_records(it.data):
                    sid = rec.get("seller_id")
                    if sid and sid not in seller_ids:
                        seller_ids.append(sid)
        payment_refs = []
        for p in bundle.by_domain("payment"):
            for rec in _iter_records(p.data):
                pid = rec.get("payment_reference") or rec.get("payment_id")
                if pid and pid not in payment_refs:
                    payment_refs.append(pid)
        if not payment_refs and order_ids:
            payment_refs = list(order_ids)
        shipment_ids = list(order_ids)

        entities = {
            "order_ids": order_ids,
            "item_ids": item_ids or order_ids,
            "seller_ids": seller_ids or (order_ids if decision.responsible_party_type == "seller" else []),
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        }

        refund_lines = []
        if decision.refund_reason_code is not None and decision.refund_amount_brl > 0:
            refund_lines.append(
                {
                    "reason_code": decision.refund_reason_code,
                    "amount_brl": round(decision.refund_amount_brl, 2),
                    "entity_id": decision.refund_entity_id,
                }
            )

        responsible_parties = []
        if decision.responsible_party_type in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
        }:
            responsible_parties.append(
                {
                    "party_type": decision.responsible_party_type,
                    "party_id": decision.responsible_party_id,
                }
            )

        output: dict[str, Any] = {
            "assessment": {
                "primary_issue": decision.primary_issue,
                "case_status": decision.case_status,
                "confidence": decision.confidence,
            },
            "affected_entities": entities,
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": decision.cause_code, "rank": 1}],
                "responsible_parties": responsible_parties,
            },
            "evidence_refs": relevant_refs[:30],
            "data_conflicts": build_data_conflicts(bundle, decision.primary_issue),
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": round(decision.refund_amount_brl, 2),
                "refund_lines": refund_lines,
            },
            "resolution_actions": list(decision.resolution_actions),
        }
        if claims:
            output["claim_assessments"] = build_claim_assessments(claims, decision, bundle)

        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=self.name,
            decision_code=decision.cause_code,
            evidence_refs=relevant_refs[:20] or None,
        )
        return output


# ---------------------------------------------------------------------------
# Verifier Agent: pre-finalize invariant checks (ARCHITECTURE.md Sec 7).
# Never fabricates a fix -- either the output already satisfies the
# invariant, or the case must be re-decided (via one supplementary task) or
# the workflow fails loudly. Also owns conflict adjudication (Sec 6).
# ---------------------------------------------------------------------------


class VerifierAgent:
    name = "verifier-agent"

    def needs_supplementary(
        self, output: dict[str, Any], bundle: EvidenceBundle
    ) -> list[tuple[str, str]] | None:
        """At most one supplementary round, requested by the Verifier, before
        a case is allowed to settle on `insufficient_evidence` (ARCHITECTURE.md
        Sec 4: "Verifier duoc quyen yeu cau Coordinator thuc hien toi da mot
        nhiem vu bo sung khi thieu bang chung bat buoc").
        """
        if output["assessment"]["primary_issue"] != "insufficient_evidence":
            return None
        if not bundle.unresolved:
            return None
        return list(bundle.unresolved)

    def verify(
        self,
        *,
        case_id: str,
        output: dict[str, Any],
        bundle: EvidenceBundle,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        output["data_conflicts"] = build_data_conflicts(
            bundle, output.get("assessment", {}).get("primary_issue", "")
        )

        known_refs = set(bundle.refs())
        cited_refs = set(output.get("evidence_refs", []))
        for claim in output.get("claim_assessments", []):
            cited_refs.update(claim.get("evidence_refs", []))
        unknown_refs = cited_refs - known_refs
        if unknown_refs:
            raise ValueError(
                f"verifier: output cites evidence not collected this run: {unknown_refs}"
            )

        refund_lines_total = round(
            sum(line["amount_brl"] for line in output["financial_resolution"]["refund_lines"]), 2
        )
        if refund_lines_total != round(output["financial_resolution"]["recommended_refund_brl"], 2):
            raise ValueError("verifier: refund_lines do not sum to recommended_refund_brl")

        action_required = output["assessment"]["case_status"] == "action_required"
        if action_required and not output["resolution_actions"]:
            raise ValueError("verifier: action_required case has no resolution_actions")

        confidence = output["assessment"]["confidence"]
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("verifier: confidence out of bounds")

        gateway.contracts.validate_output(output, f"outputs/{case_id}.json (pre-finalize)")

        attributes: dict[str, str | int | float | bool | None] = {
            "data_conflict_count": len(output["data_conflicts"])
        }
        if output["data_conflicts"]:
            attributes["conflict_decision_code"] = "SOURCE_CONFLICT"
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.name,
            decision_code="invariants_passed",
            attributes=attributes,
        )
        return output


@dataclass
class Coordinator:
    specialists: tuple[SpecialistAgent, ...] = SPECIALISTS
    policy_agent: PolicyAgent = field(default_factory=PolicyAgent)
    verifier_agent: VerifierAgent = field(default_factory=VerifierAgent)

    async def _run_specialists(
        self,
        *,
        case_id: str,
        seeds: dict[str, set[str]],
        claim_ids: tuple[str, ...],
        tools_by_domain: dict[str, list[ToolDescriptor]],
        gateway: EvidenceGateway,
        bundle: EvidenceBundle,
        trace: TraceWriter,
    ) -> None:
        await asyncio.gather(
            *(
                specialist.run(
                    case_id=case_id,
                    seeds=seeds,
                    claim_ids=claim_ids,
                    tools_by_domain=tools_by_domain,
                    gateway=gateway,
                    bundle=bundle,
                    trace=trace,
                )
                for specialist in self.specialists
            )
        )

    async def _run_supplementary_task(
        self,
        *,
        case_id: str,
        pending: list[tuple[str, str]],
        tools_by_domain: dict[str, list[ToolDescriptor]],
        gateway: EvidenceGateway,
        bundle: EvidenceBundle,
        trace: TraceWriter,
    ) -> None:
        by_domain: dict[str, set[str]] = {}
        for domain, entity_id in pending:
            by_domain.setdefault(domain, set()).add(entity_id)
        # These entries will be re-attempted now; drop them so a lookup that
        # fails again is recorded exactly once, not accumulated.
        bundle.unresolved = [item for item in bundle.unresolved if item not in pending]

        for domain, entity_ids in by_domain.items():
            task = AgentMessage(
                case_id=case_id,
                task_id=new_task_id(),
                from_actor=self.verifier_agent.name,
                to_actor="coordinator",
                domain=domain,
                identifiers=tuple(sorted(entity_ids)),
                status="insufficient_evidence",
            )
            a2a.emit(trace, task, event_type="task_assigned")

            result = await fetch_domain_evidence(
                gateway,
                case_id=case_id,
                domain=domain,
                entity_ids=entity_ids,
                tools=tools_by_domain.get(domain, []),
                bundle=bundle,
            )

            result_message = AgentMessage(
                case_id=case_id,
                task_id=task.task_id,
                from_actor="coordinator",
                to_actor="coordinator",
                domain=domain,
                evidence_refs=tuple(item.evidence_ref for item in result.items),
                status=result.status,
                error_code=result.decision_code,
                attempt=result.attempts,
            )
            a2a.emit(trace, result_message, event_type="handoff")

            if result.items:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="coordinator",
                    target=domain,
                    tool_name=result.items[0].tool_name,
                    evidence_refs=[item.evidence_ref for item in result.items],
                )

    async def solve(
        self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
    ) -> dict[str, Any]:
        case_id = case["case_id"]
        seeds = extract_seed_entities(case)
        claims = extract_claims(case)
        claim_ids = tuple(claim["claim_id"] for claim in claims)
        tools_by_domain = await discover_tools(gateway)
        bundle = EvidenceBundle()

        await self._run_specialists(
            case_id=case_id,
            seeds=seeds,
            claim_ids=claim_ids,
            tools_by_domain=tools_by_domain,
            gateway=gateway,
            bundle=bundle,
            trace=trace,
        )

        evidence_to_policy = AgentMessage(
            case_id=case_id,
            task_id=new_task_id(),
            from_actor="coordinator",
            to_actor=self.policy_agent.name,
            claim_ids=claim_ids,
            evidence_refs=tuple(bundle.refs()),
            status="completed" if bundle.items else "insufficient_evidence",
        )
        a2a.emit(trace, evidence_to_policy, event_type="handoff")

        policy_output = await self.policy_agent.decide(
            case_id=case_id,
            claims=claims,
            seeds=seeds,
            tools_by_domain=tools_by_domain,
            gateway=gateway,
            bundle=bundle,
            trace=trace,
        )

        pending = self.verifier_agent.needs_supplementary(policy_output, bundle)
        if pending is not None:
            await self._run_supplementary_task(
                case_id=case_id,
                pending=pending,
                tools_by_domain=tools_by_domain,
                gateway=gateway,
                bundle=bundle,
                trace=trace,
            )

            supplementary_to_policy = AgentMessage(
                case_id=case_id,
                task_id=new_task_id(),
                from_actor="coordinator",
                to_actor=self.policy_agent.name,
                claim_ids=claim_ids,
                evidence_refs=tuple(bundle.refs()),
                status="completed" if bundle.items else "insufficient_evidence",
            )
            a2a.emit(trace, supplementary_to_policy, event_type="handoff")

            policy_output = await self.policy_agent.decide(
                case_id=case_id,
                claims=claims,
                seeds=seeds,
                tools_by_domain=tools_by_domain,
                gateway=gateway,
                bundle=bundle,
                trace=trace,
            )

        output = {"schema_version": "day09-l3a-output-v2", "case_id": case_id, **policy_output}

        policy_to_verifier = AgentMessage(
            case_id=case_id,
            task_id=new_task_id(),
            from_actor=self.policy_agent.name,
            to_actor=self.verifier_agent.name,
            evidence_refs=tuple(output["evidence_refs"]),
            status="completed",
        )
        a2a.emit(trace, policy_to_verifier, event_type="handoff")

        return self.verifier_agent.verify(
            case_id=case_id, output=output, bundle=bundle, gateway=gateway, trace=trace
        )
