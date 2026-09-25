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
    extract_known_ids,
    fetch_domain_evidence,
    resolve_calls,
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
        known_ids: dict[str, set[str]],
        claim_ids: tuple[str, ...],
        tools_by_domain: dict[str, list[ToolDescriptor]],
        gateway: EvidenceGateway,
        bundle: EvidenceBundle,
        trace: TraceWriter,
    ) -> None:
        for domain in self.domains:
            tools = tools_by_domain.get(domain, [])
            resolved = resolve_calls(tools, known_ids)
            identifiers = tuple(sorted({entity_id for _, _, entity_id in resolved}))

            task = AgentMessage(
                case_id=case_id,
                task_id=new_task_id(),
                from_actor="coordinator",
                to_actor=self.name,
                domain=domain,
                claim_ids=claim_ids,
                identifiers=identifiers,
                status="completed",
            )
            a2a.emit(trace, task, event_type="task_assigned")

            result = await self._fetch_with_retry(
                case_id=case_id,
                domain=domain,
                known_ids=known_ids,
                tools=tools,
                gateway=gateway,
                bundle=bundle,
            )

            # Lifecycle order (Pha 4): consume the tool result before handing
            # facts off to the Coordinator, matching
            # case_received -> task_assigned -> tool_result_consumed -> handoff -> ...
            if result.items:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.name,
                    target=domain,
                    tool_name=result.items[0].tool_name,
                    evidence_refs=[item.evidence_ref for item in result.items],
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

    async def _fetch_with_retry(
        self,
        *,
        case_id: str,
        domain: str,
        known_ids: dict[str, set[str]],
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
                    known_ids=known_ids,
                    tools=tools,
                    bundle=bundle,
                )
            except Exception:  # noqa: BLE001 - deliberately broad: see docstring
                if attempt == 1:
                    for _, _, entity_id in resolve_calls(tools, known_ids):
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


def _order_status(order_items: list[Any]) -> str | None:
    for item in order_items:
        status = _get(item.data, "order_status", "status")
        if isinstance(status, str):
            return status.lower()
    return None


def _payment_total(payment_items: list[Any]) -> float | None:
    values = [
        _as_number(_get(item.data, "payment_value", "amount", "value")) for item in payment_items
    ]
    values = [value for value in values if value is not None]
    return sum(values) if values else None


def _duplicate_payment_amount(payment_items: list[Any]) -> float | None:
    seen: dict[float, int] = {}
    for item in payment_items:
        amount = _as_number(_get(item.data, "payment_value", "amount", "value"))
        if amount is None:
            continue
        seen[amount] = seen.get(amount, 0) + 1
    for amount, count in seen.items():
        if count > 1:
            return amount
    return None


def _refund_status(refund_items: list[Any]) -> str | None:
    for item in refund_items:
        status = _get(item.data, "refund_status", "status")
        if isinstance(status, str):
            return status.lower()
    return None


def _item_total(item_items: list[Any]) -> float | None:
    values = [_as_number(_get(item.data, "price", "item_price")) for item in item_items]
    values = [value for value in values if value is not None]
    return sum(values) if values else None


def _shipment_delay(shipment_items: list[Any], order_items: list[Any]) -> str | None:
    """Return 'seller' | 'logistics' | None (on-time or unknown)."""
    delivered_at = None
    estimated_at = None
    shipped_after_limit = None
    for item in shipment_items:
        delivered_at = delivered_at or _get(item.data, "delivered_at", "delivery_date")
        shipped_after_limit = (
            shipped_after_limit
            if shipped_after_limit is not None
            else _get(item.data, "shipped_after_limit")
        )
    for item in order_items:
        estimated_at = estimated_at or _get(
            item.data, "order_estimated_delivery_date", "estimated_delivery_date"
        )
        delivered_at = delivered_at or _get(
            item.data, "order_delivered_customer_date", "delivered_at"
        )
    if not delivered_at or not estimated_at:
        return None
    if delivered_at <= estimated_at:
        return None
    if shipped_after_limit is True:
        return "seller"
    return "logistics"


def _cancellation_responsible_party(order_items: list[Any]) -> str:
    """Attribute a cancellation/unavailability to a party from evidence when
    the order data says so explicitly; otherwise default to "seller" (the
    common cause in a marketplace) rather than guessing "platform" for one
    branch and "seller" for the other with no evidentiary basis.
    """
    for item in order_items:
        marker = _get(item.data, "canceled_by", "cancellation_reason", "cancel_reason")
        if isinstance(marker, str) and "platform" in marker.lower():
            return "platform"
    return "seller"


def decide(claims: list[dict[str, Any]], bundle: EvidenceBundle) -> Decision:
    order_items = bundle.by_domain("order")
    item_items = bundle.by_domain("item")
    payment_items = bundle.by_domain("payment")
    shipment_items = bundle.by_domain("shipment")
    refund_items = bundle.by_domain("refund")

    order_status = _order_status(order_items)
    payment_total = _payment_total(payment_items)
    item_total = _item_total(item_items)
    duplicate_amount = _duplicate_payment_amount(payment_items)
    refund_status = _refund_status(refund_items)
    delay_owner = _shipment_delay(shipment_items, order_items)

    order_id = order_items[0].entity_id if order_items else None
    seller_items = bundle.by_domain("seller")
    seller_id = seller_items[0].entity_id if seller_items else None
    payment_ref = payment_items[0].entity_id if payment_items else None
    refund_id = refund_items[0].entity_id if refund_items else order_id

    order_paid = payment_total is not None and payment_total > 0
    if order_status in {"canceled", "cancelled"} and order_paid:
        return Decision(
            "canceled_order_paid",
            "action_required",
            0.85,
            "ORDER_CANCELED_AFTER_CAPTURE",
            _cancellation_responsible_party(order_items),
            seller_id,
            ("issue_full_refund", "notify_customer"),
            "order_not_fulfilled",
            payment_total,
            order_id,
            relevant_domains=("order", "payment"),
        )

    if order_status == "unavailable" and order_paid:
        return Decision(
            "unavailable_order_paid",
            "action_required",
            0.8,
            "ORDER_UNAVAILABLE_AFTER_CAPTURE",
            _cancellation_responsible_party(order_items),
            seller_id,
            ("issue_full_refund", "notify_customer"),
            "order_not_fulfilled",
            payment_total,
            order_id,
            relevant_domains=("order", "payment"),
        )

    if duplicate_amount is not None:
        return Decision(
            "duplicate_charge",
            "action_required",
            0.75,
            "DUPLICATE_PAYMENT_CAPTURE",
            "payment_provider",
            payment_ref,
            ("reverse_duplicate_charge", "notify_customer"),
            "duplicate_capture_reversal",
            duplicate_amount,
            payment_ref,
            relevant_domains=("payment",),
        )

    if refund_status in {"pending", "processing"}:
        return Decision(
            "refund_pending",
            "needs_investigation",
            0.6,
            "REFUND_IN_PROGRESS",
            "payment_provider",
            refund_id,
            ("monitor_refund_status",),
            None,
            0.0,
            None,
            relevant_domains=("refund",),
        )

    if refund_status in {"failed", "rejected"}:
        rejected_amount = 0.0
        if refund_items:
            rejected_amount = (
                _as_number(_get(refund_items[0].data, "refund_amount", "amount")) or 0.0
            )
        return Decision(
            "refund_failed",
            "action_required",
            0.7,
            "REFUND_ATTEMPT_REJECTED",
            "payment_provider",
            refund_id,
            ("retry_refund", "notify_customer"),
            "refund_retry_required",
            rejected_amount,
            refund_id,
            relevant_domains=("refund",),
        )

    if delay_owner == "seller":
        return Decision(
            "late_delivery_seller",
            "action_required",
            0.65,
            "SELLER_SHIP_AFTER_DEADLINE",
            "seller",
            seller_id,
            ("escalate_to_seller", "notify_customer"),
            None,
            0.0,
            None,
            relevant_domains=("shipment", "order"),
        )

    if delay_owner == "logistics":
        return Decision(
            "late_delivery_logistics",
            "action_required",
            0.6,
            "CARRIER_TRANSIT_DELAY",
            "logistics_provider",
            None,
            ("escalate_to_logistics_provider", "notify_customer"),
            None,
            0.0,
            None,
            relevant_domains=("shipment", "order"),
        )

    totals_known = payment_total is not None and item_total is not None
    totals_mismatch = totals_known and abs(payment_total - item_total) > 0.01
    if totals_mismatch:
        return Decision(
            "payment_mismatch",
            "needs_investigation",
            0.55,
            "PAYMENT_TOTAL_MISMATCH",
            "payment_provider",
            payment_ref,
            ("reconcile_payment_ledger", "notify_finance_team"),
            "payment_reconciliation_adjustment",
            abs(payment_total - item_total),
            payment_ref,
            relevant_domains=("payment", "item"),
        )

    if order_status == "delivered" and payment_total is not None and item_total is not None:
        return Decision(
            "valid_split_payment",
            "no_action",
            0.7,
            "PAYMENT_MATCHES_ORDER",
            "unknown",
            None,
            ("close_case_no_action",),
            None,
            0.0,
            None,
            relevant_domains=("order", "payment", "item"),
        )

    if claims and not bundle.items:
        return Decision(
            "unsupported_claim",
            "no_action",
            0.4,
            "CLAIM_NOT_CORROBORATED",
            "unknown",
            None,
            ("close_case_no_action", "notify_customer"),
            None,
            0.0,
            None,
        )

    return Decision(
        "insufficient_evidence",
        "needs_investigation",
        0.2,
        "EVIDENCE_GAP",
        "unknown",
        None,
        ("open_investigation",),
        None,
        0.0,
        None,
    )


def build_data_conflicts(bundle: EvidenceBundle) -> list[dict[str, Any]]:
    """Detect and adjudicate conflicting evidence sources.

    ARCHITECTURE.md Sec 6 says specialists must not pick a source themselves
    on conflict; adjudication happens here, in the Verifier's own pass (see
    VerifierAgent.verify), right before the schema's required
    `selected_source` / `resolution_code` are written to the output.
    """
    conflicts: list[dict[str, Any]] = []
    order_status = _order_status(bundle.by_domain("order"))
    shipment_items = bundle.by_domain("shipment")
    shipment_status = None
    for item in shipment_items:
        shipment_status = _get(item.data, "shipment_status", "status")
        if shipment_status:
            break
    if order_status == "delivered" and shipment_status and str(shipment_status).lower() not in {
        "delivered",
        "completed",
    }:
        conflicts.append(
            {
                "field": "delivery_status",
                "sources": ["order", "shipment"],
                "selected_source": "shipment",
                "resolution_code": "prefer_shipment_domain_of_record",
            }
        )
    payment_total = _payment_total(bundle.by_domain("payment"))
    item_total = _item_total(bundle.by_domain("item"))
    totals_known = payment_total is not None and item_total is not None
    if totals_known and abs(payment_total - item_total) > 0.01:
        conflicts.append(
            {
                "field": "order_total",
                "sources": ["payment", "item"],
                "selected_source": "payment",
                "resolution_code": "prefer_payment_ledger",
            }
        )
    return conflicts[:5]


def build_claim_assessments(
    claims: list[dict[str, Any]], decision: Decision, bundle: EvidenceBundle
) -> list[dict[str, Any]]:
    # Pha 3 rule 3: only cite evidence that actually supports the conclusion --
    # never the full bundle, which may include evidence from unrelated domains.
    relevant_refs = bundle.refs_for(decision.relevant_domains)
    assessments = []
    for claim in claims:
        if not bundle.items:
            verdict = "insufficient_evidence"
            confidence = 0.2
        elif decision.primary_issue in {"unsupported_claim"}:
            verdict = "unsupported"
            confidence = decision.confidence
        elif decision.primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            confidence = decision.confidence
        else:
            verdict = "supported"
            confidence = decision.confidence
        assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": relevant_refs[:30],
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
        known_ids: dict[str, set[str]],
        tools_by_domain: dict[str, list[ToolDescriptor]],
        gateway: EvidenceGateway,
        bundle: EvidenceBundle,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        # The Policy Agent holds MCP permission for the "policy" domain
        # (ARCHITECTURE.md Sec 3) but only exercises it once a rule in
        # decide() actually reconciles facts against policy evidence.
        # Acquisition and consumption of evidence must stay paired (Sec 5):
        # fetching a "policy" fact that no rule reads would let the Policy
        # Agent emit `tool_result_consumed` for evidence that never ends up
        # supporting the conclusion -- exactly what Pha 3 rule 3 forbids.
        del known_ids, tools_by_domain, gateway  # reserved for future policy rules
        decision = decide(claims, bundle)
        relevant_refs = bundle.refs_for(decision.relevant_domains)

        entities = {
            "order_ids": sorted({item.entity_id for item in bundle.by_domain("order")}),
            "item_ids": sorted({item.entity_id for item in bundle.by_domain("item")}),
            "seller_ids": sorted({item.entity_id for item in bundle.by_domain("seller")}),
            "payment_references": sorted({item.entity_id for item in bundle.by_domain("payment")}),
            "shipment_ids": sorted({item.entity_id for item in bundle.by_domain("shipment")}),
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
        if decision.responsible_party_type != "unknown" or decision.responsible_party_id:
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
            "data_conflicts": [],
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
# the workflow fails loudly. Also owns conflict adjudication (Sec 6) and
# confidence calibration (Pha 4).
# ---------------------------------------------------------------------------

# Which responsible_party_type values are logically compatible with a given
# primary_issue (Pha 4: "loi do nguoi ban thi don vi van chuyen khong the
# chiu trach nhiem hoan tien"). "unknown" is always a safe fallback when the
# Policy Agent found no evidence-backed party to name.
ALLOWED_RESPONSIBLE_PARTIES: dict[str, frozenset[str]] = {
    "canceled_order_paid": frozenset({"seller", "platform", "unknown"}),
    "unavailable_order_paid": frozenset({"seller", "platform", "unknown"}),
    "late_delivery_seller": frozenset({"seller", "unknown"}),
    "late_delivery_logistics": frozenset({"logistics_provider", "unknown"}),
    "valid_split_payment": frozenset({"unknown"}),
    "payment_mismatch": frozenset({"payment_provider", "unknown"}),
    "duplicate_charge": frozenset({"payment_provider", "unknown"}),
    "refund_pending": frozenset({"payment_provider", "unknown"}),
    "refund_failed": frozenset({"payment_provider", "unknown"}),
    "unsupported_claim": frozenset({"unknown"}),
    "insufficient_evidence": frozenset({"unknown"}),
}


def _check_responsibility_consistency(output: dict[str, Any]) -> None:
    primary_issue = output["assessment"]["primary_issue"]
    allowed = ALLOWED_RESPONSIBLE_PARTIES.get(primary_issue)
    if allowed is None:
        return
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] not in allowed:
            raise ValueError(
                f"verifier: primary_issue={primary_issue!r} cannot assign "
                f"responsible party {party['party_type']!r}"
            )
    refund_brl = output["financial_resolution"]["recommended_refund_brl"]
    responsible_types = {
        party["party_type"] for party in output["root_cause_analysis"]["responsible_parties"]
    }
    if refund_brl > 0 and not (responsible_types - {"unknown"}):
        raise ValueError("verifier: a recommended refund has no named responsible party")


def _calibrate_confidence(
    base_confidence: float, bundle: EvidenceBundle, conflicts: list[dict[str, Any]]
) -> float:
    """Pha 4: confidence must reflect evidence quality/completeness, never an
    optimistic flat number. Only ever adjusts downward from the Policy
    Agent's rule-based estimate -- evidence gaps or conflicts can erode trust
    in a conclusion, but nothing here can manufacture certainty that wasn't
    earned by evidence.
    """
    penalty = 0.0
    if conflicts:
        penalty += min(0.15 * len(conflicts), 0.3)
    total_lookups = len(bundle.items) + len(bundle.unresolved)
    if total_lookups > 0 and bundle.unresolved:
        gap_ratio = len(bundle.unresolved) / total_lookups
        penalty += gap_ratio * 0.2
    return round(max(0.0, min(1.0, base_confidence - penalty)), 2)


class VerifierAgent:
    name = "verifier-agent"

    def needs_supplementary(
        self, output: dict[str, Any], bundle: EvidenceBundle
    ) -> list[tuple[str, str, str]] | None:
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
        output["data_conflicts"] = build_data_conflicts(bundle)

        original_confidence = output["assessment"]["confidence"]
        calibrated_confidence = _calibrate_confidence(
            original_confidence, bundle, output["data_conflicts"]
        )
        output["assessment"]["confidence"] = calibrated_confidence

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

        _check_responsibility_consistency(output)

        confidence = output["assessment"]["confidence"]
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("verifier: confidence out of bounds")

        gateway.contracts.validate_output(output, f"outputs/{case_id}.json (pre-finalize)")

        attributes: dict[str, str | int | float | bool | None] = {
            "data_conflict_count": len(output["data_conflicts"]),
            "confidence_calibrated": calibrated_confidence != original_confidence,
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
        known_ids: dict[str, set[str]],
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
                    known_ids=known_ids,
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
        pending: list[tuple[str, str, str]],
        tools_by_domain: dict[str, list[ToolDescriptor]],
        gateway: EvidenceGateway,
        bundle: EvidenceBundle,
        trace: TraceWriter,
    ) -> None:
        by_domain: dict[str, dict[str, set[str]]] = {}
        for domain, id_param, entity_id in pending:
            by_domain.setdefault(domain, {}).setdefault(id_param, set()).add(entity_id)
        # These entries will be re-attempted now; drop them so a lookup that
        # fails again is recorded exactly once, not accumulated.
        bundle.unresolved = [item for item in bundle.unresolved if item not in pending]

        for domain, known_ids in by_domain.items():
            identifiers = tuple(sorted({eid for ids in known_ids.values() for eid in ids}))
            task = AgentMessage(
                case_id=case_id,
                task_id=new_task_id(),
                from_actor=self.verifier_agent.name,
                to_actor="coordinator",
                domain=domain,
                identifiers=identifiers,
                status="insufficient_evidence",
            )
            a2a.emit(trace, task, event_type="task_assigned")

            result = await fetch_domain_evidence(
                gateway,
                case_id=case_id,
                domain=domain,
                known_ids=known_ids,
                tools=tools_by_domain.get(domain, []),
                bundle=bundle,
            )

            if result.items:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="coordinator",
                    target=domain,
                    tool_name=result.items[0].tool_name,
                    evidence_refs=[item.evidence_ref for item in result.items],
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

    async def solve(
        self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
    ) -> dict[str, Any]:
        case_id = case["case_id"]
        known_ids = extract_known_ids(case)
        claims = extract_claims(case)
        claim_ids = tuple(claim["claim_id"] for claim in claims)
        tools_by_domain = await discover_tools(gateway)
        bundle = EvidenceBundle()

        await self._run_specialists(
            case_id=case_id,
            known_ids=known_ids,
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
            known_ids=known_ids,
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
                known_ids=known_ids,
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
