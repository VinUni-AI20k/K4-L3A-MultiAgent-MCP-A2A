"""L3A coordinator, specialist agents and verifier.

Agents talk through `Message` envelopes correlated by `case_id`. Each agent may only call
the MCP tools listed in `TOOL_GRANTS`; every evidence ref is recorded in a per-case ledger
and only ledger refs can be cited in the output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .rules import (
    NO_ACTION_ISSUES,
    Facts,
    Window,
    choose_primary,
    detect_issues,
    full_refund_verdict,
    one_row_per_item,
    parse_time,
    refund_amount,
    responsible_parties,
    split_in_window,
)
from .trace import TraceWriter

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

TOOL_GRANTS = {
    ORDER_AGENT: {"get_order", "get_order_items", "get_sellers"},
    PAYMENT_AGENT: {"get_payment_timeline", "get_refund_timeline"},
    SHIPMENT_AGENT: {"get_shipment_summary"},
    POLICY_AGENT: {"get_policy"},
}

# Evidence groups cited for each primary issue (keys of CaseContext.refs).
CITATIONS = {
    "canceled_order_paid": ("order", "payment", "refund", "policy"),
    "unavailable_order_paid": ("order", "items", "sellers", "payment", "refund", "policy"),
    "late_delivery_seller": ("order", "items", "sellers", "shipment", "policy"),
    "late_delivery_logistics": ("order", "shipment", "policy"),
    "valid_split_payment": ("order", "items", "payment", "policy"),
    "payment_mismatch": ("order", "items", "payment", "policy"),
    "duplicate_charge": ("order", "items", "payment", "policy"),
    "refund_pending": ("order", "payment", "refund", "policy"),
    "refund_failed": ("order", "payment", "refund", "policy"),
    "unsupported_claim": ("order", "shipment", "payment", "policy"),
    "insufficient_evidence": ("order", "payment", "shipment", "policy"),
}
REFUND_CLAIM_CITATIONS = ("payment", "refund", "policy")

MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0


class EvidenceUnavailable(Exception):
    """The gateway answered that the requested evidence does not exist for this scope."""


@dataclass(frozen=True)
class Message:
    case_id: str
    sender: str
    recipient: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseContext:
    case: dict[str, Any]
    trace: TraceWriter
    refs: dict[str, str] = field(default_factory=dict)
    ledger: dict[str, str] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    @property
    def order_id(self) -> str:
        return self.case["customer_request"]["claimed_order_id"]


class Agent:
    name = ""

    def __init__(self, gateway: EvidenceGateway, context: CaseContext) -> None:
        self._gateway = gateway
        self.context = context

    async def fetch(self, key: str, tool_name: str, **arguments: str) -> Any:
        """Call a granted tool with bounded retries, then record and trace the evidence."""
        if tool_name not in TOOL_GRANTS.get(self.name, set()):
            raise PermissionError(f"{self.name} is not granted {tool_name}")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                evidence = await self._gateway.call(
                    tool_name, case_id=self.context.case_id, **arguments
                )
                break
            except RuntimeError as exc:
                raise EvidenceUnavailable(str(exc)) from exc
            except ValueError:
                raise
            except Exception:
                if attempt == MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(RETRY_BASE_SECONDS * attempt)
        ref = evidence["evidence_ref"]
        self.context.refs[key] = ref
        self.context.ledger[ref] = self.context.case_id
        self.context.trace.emit(
            case_id=self.context.case_id,
            event_type="tool_result_consumed",
            actor=self.name,
            tool_name=tool_name,
            evidence_refs=[ref],
            attributes={
                "domain": evidence["domain"],
                "warnings": len(evidence.get("warnings", [])),
            },
        )
        return evidence["data"]


class OrderAgent(Agent):
    name = ORDER_AGENT

    async def handle(self, message: Message) -> dict[str, Any]:
        order = await self.fetch("order", "get_order", order_id=self.context.order_id)
        if not isinstance(order, dict) or order.get("order_id") != self.context.order_id:
            raise EvidenceUnavailable("order evidence does not match the claimed order")
        start = parse_time(order.get("order_purchase_timestamp"))
        end = parse_time(self.context.case.get("opened_at"))
        if start is None or end is None or start > end:
            raise EvidenceUnavailable("order has no usable purchase/opened window")
        window = Window(start, end)
        items = await self.fetch("items", "get_order_items", order_id=self.context.order_id)
        in_window, dropped = split_in_window(items or [], "shipping_limit_date", window)
        kept, repeated = one_row_per_item(in_window)
        return {
            "order": order,
            "window": window,
            "items": kept,
            "excluded_items": dropped + repeated,
        }

    async def resolve_sellers(self, seller_ids: list[str]) -> list[str]:
        sellers = await self.fetch("sellers", "get_sellers", order_id=self.context.order_id)
        known = {seller.get("seller_id") for seller in sellers or []}
        return [seller for seller in seller_ids if seller in known]


class PaymentAgent(Agent):
    name = PAYMENT_AGENT

    async def handle(self, message: Message) -> dict[str, Any]:
        window: Window = message.payload["window"]
        timeline = await self.fetch(
            "payment", "get_payment_timeline", order_id=self.context.order_id
        )
        events, dropped_payments = split_in_window(timeline.get("events") or [], "event_at", window)
        try:
            refund = await self.fetch(
                "refund", "get_refund_timeline", order_id=self.context.order_id
            )
            refund_events = refund.get("events") or []
        except EvidenceUnavailable:
            refund_events = []
        refunds, dropped_refunds = split_in_window(refund_events, "event_at", window)
        return {
            "captures": [event for event in events if event.get("event_type") == "captured"],
            "mismatches": [
                event for event in events if event.get("event_type") == "reconciliation_mismatch"
            ],
            "refunds": refunds,
            "excluded_payment_events": dropped_payments,
            "excluded_refund_events": dropped_refunds,
        }


class ShipmentAgent(Agent):
    name = SHIPMENT_AGENT

    async def handle(self, message: Message) -> dict[str, Any]:
        window: Window = message.payload["window"]
        summary = await self.fetch(
            "shipment", "get_shipment_summary", order_id=self.context.order_id
        )
        events, dropped = split_in_window(summary.get("events") or [], "event_at", window)
        return {
            "delivered_carrier_at": parse_time(summary.get("delivered_carrier_at")),
            "delivered_customer_at": parse_time(summary.get("delivered_customer_at")),
            "estimated_delivery_at": parse_time(summary.get("estimated_delivery_at")),
            "events": events,
            "excluded_shipment_events": dropped,
        }


class PolicyAgent(Agent):
    name = POLICY_AGENT

    async def load(self) -> dict[str, Any]:
        policy = await self.fetch(
            "policy", "get_policy", policy_version=self.context.case["policy_version"]
        )
        return policy.get("rules") or {}


class Coordinator:
    def __init__(self, gateway: EvidenceGateway, context: CaseContext) -> None:
        self.context = context
        self.order = OrderAgent(gateway, context)
        self.payment = PaymentAgent(gateway, context)
        self.shipment = ShipmentAgent(gateway, context)
        self.policy = PolicyAgent(gateway, context)

    def _emit(self, event_type: str, actor: str, **fields: Any) -> None:
        self.context.trace.emit(
            case_id=self.context.case_id, event_type=event_type, actor=actor, **fields
        )

    async def delegate(self, agent: Agent, kind: str, **payload: Any) -> dict[str, Any]:
        message = Message(self.context.case_id, COORDINATOR, agent.name, kind, payload)
        self._emit("task_assigned", COORDINATOR, target=agent.name, decision_code=kind)
        report = await agent.handle(message)
        self._emit("handoff", agent.name, target=COORDINATOR, decision_code=f"{kind}_DONE")
        return report

    async def solve(self) -> dict[str, Any]:
        claims = self.context.case["customer_request"].get("claims") or []
        claimed_topic = claims[0]["topic"] if claims else None
        facts = Facts(order_id=self.context.order_id)
        try:
            order_report = await self.delegate(self.order, "ORDER_CONTEXT")
            window = order_report["window"]
            payment_report, shipment_report = await asyncio.gather(
                self.delegate(self.payment, "PAYMENT_RECONCILIATION", window=window),
                self.delegate(self.shipment, "SHIPMENT_TIMELINE", window=window),
            )
        except EvidenceUnavailable as exc:
            self._emit(
                "handoff",
                COORDINATOR,
                target=VERIFIER,
                decision_code="EVIDENCE_UNAVAILABLE",
                attributes={"detail": str(exc)[:160]},
            )
            rules = await self._policy_rules()
            return self._finalize("insufficient_evidence", 0.5, {}, facts, rules, claims)

        facts.order_status = order_report["order"].get("order_status")
        facts.items = order_report["items"]
        facts.captures = payment_report["captures"]
        facts.mismatches = payment_report["mismatches"]
        facts.refunds = payment_report["refunds"]
        facts.shipment_events = shipment_report["events"]
        facts.delivered_carrier_at = shipment_report["delivered_carrier_at"]
        facts.delivered_customer_at = shipment_report["delivered_customer_at"]
        facts.estimated_delivery_at = shipment_report["estimated_delivery_at"]
        facts.excluded = {
            "get_order_items": order_report["excluded_items"],
            "get_payment_timeline": payment_report["excluded_payment_events"],
            "get_refund_timeline": payment_report["excluded_refund_events"],
            "get_shipment_summary": shipment_report["excluded_shipment_events"],
        }

        found = detect_issues(facts)
        primary, confidence = choose_primary(found, claimed_topic)
        rules = await self._policy_rules()
        decision = self._finalize(primary, confidence, found, facts, rules, claims)
        if any(party["party_type"] == "seller" for party in decision["parties"]):
            decision["parties"] = await self._verify_sellers(decision["parties"], facts)
        return decision

    async def _policy_rules(self) -> dict[str, Any]:
        self._emit("task_assigned", COORDINATOR, target=POLICY_AGENT, decision_code="POLICY_LOOKUP")
        try:
            rules = await self.policy.load()
        except EvidenceUnavailable:
            rules = {}
        self._emit(
            "handoff",
            POLICY_AGENT,
            target=COORDINATOR,
            decision_code="POLICY_LOOKUP_DONE" if rules else "POLICY_UNAVAILABLE",
        )
        return rules

    async def _verify_sellers(
        self, parties: list[dict[str, str | None]], facts: Facts
    ) -> list[dict[str, str | None]]:
        self._emit("task_assigned", COORDINATOR, target=ORDER_AGENT, decision_code="SELLER_CHECK")
        try:
            verified = set(await self.order.resolve_sellers(facts.seller_ids))
        except EvidenceUnavailable:
            verified = set()
        self._emit("handoff", ORDER_AGENT, target=COORDINATOR, decision_code="SELLER_CHECK_DONE")
        kept = [
            party
            for party in parties
            if party["party_type"] != "seller" or party["party_id"] in verified
        ]
        return kept or [{"party_type": "unknown", "party_id": None}]

    def _finalize(
        self,
        primary: str,
        confidence: float,
        found: dict[str, str],
        facts: Facts,
        rules: dict[str, Any],
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rule = rules.get(primary) or {
            "case_status": "needs_investigation",
            "recommended_action": "escalate_manual_review",
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        }
        refund = refund_amount(primary, facts)
        parties = responsible_parties(rule.get("responsible_parties") or [], facts)
        self._emit(
            "policy_decided",
            POLICY_AGENT,
            decision_code=primary.upper(),
            evidence_refs=[self.context.refs["policy"]] if "policy" in self.context.refs else None,
            attributes={
                "case_status": rule["case_status"],
                "action": rule["recommended_action"],
                "refund_brl": float(refund),
            },
        )
        return {
            "primary": primary,
            "confidence": confidence,
            "found": found,
            "facts": facts,
            "rule": rule,
            "refund": refund,
            "parties": parties,
            "claims": claims,
        }


def _cite(context: CaseContext, keys: tuple[str, ...]) -> list[str]:
    return [context.refs[key] for key in keys if key in context.refs]


def build_output(context: CaseContext, decision: dict[str, Any]) -> dict[str, Any]:
    primary: str = decision["primary"]
    facts: Facts = decision["facts"]
    rule: dict[str, Any] = decision["rule"]
    refund: Decimal = decision["refund"]
    confidence: float = decision["confidence"]
    cited = _cite(context, CITATIONS[primary])

    claim_assessments = []
    for claim in decision["claims"][:5]:
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            verdict = full_refund_verdict(refund, facts)
            refs = _cite(context, REFUND_CLAIM_CITATIONS)
        elif primary == "insufficient_evidence":
            verdict, refs = "insufficient_evidence", cited
        else:
            confirmed = topic == primary and topic != "unsupported_claim"
            verdict = "supported" if confirmed else "unsupported"
            refs = cited
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )

    conflicts = [
        {
            "field": f"{tool}.records",
            "sources": [f"{tool}:in_case_window", f"{tool}:out_of_case_window"],
            "selected_source": f"{tool}:in_case_window",
            "resolution_code": "EXCLUDED_OUT_OF_CASE_WINDOW",
        }
        for tool, count in facts.excluded.items()
        if count
    ][:5]

    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": rule["recommended_action"].upper(),
                "amount_brl": float(refund),
                "entity_id": facts.order_id,
            }
        )

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": context.case_id,
        "assessment": {
            "primary_issue": primary,
            "case_status": rule["case_status"],
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [facts.order_id],
            "item_ids": facts.item_ids,
            "seller_ids": facts.seller_ids,
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary.upper(), "rank": 1}],
            "responsible_parties": decision["parties"],
        },
        "evidence_refs": cited,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [rule["recommended_action"]],
    }


def verify(context: CaseContext, output: dict[str, Any]) -> list[str]:
    """Return violated invariants; an empty list means the output may be finalized."""
    problems: list[str] = []
    cited = set(output["evidence_refs"])
    for claim in output.get("claim_assessments", []):
        cited.update(claim["evidence_refs"])
    if any(context.ledger.get(ref) != context.case_id for ref in cited):
        problems.append("EVIDENCE_OUT_OF_SCOPE")
    if not output["evidence_refs"]:
        problems.append("NO_EVIDENCE")
    assessment = output["assessment"]
    financial = output["financial_resolution"]
    lines_total = sum(line["amount_brl"] for line in financial["refund_lines"])
    if abs(lines_total - financial["recommended_refund_brl"]) > 0.005:
        problems.append("REFUND_LINES_MISMATCH")
    if assessment["primary_issue"] in NO_ACTION_ISSUES and financial["recommended_refund_brl"]:
        problems.append("NO_ACTION_WITH_REFUND")
    if assessment["case_status"] == "no_action" and financial["recommended_refund_brl"]:
        problems.append("STATUS_REFUND_INCONSISTENT")
    if output["affected_entities"]["order_ids"] != [context.order_id]:
        problems.append("ENTITY_SCOPE")
    if not 0 <= assessment["confidence"] <= 1:
        problems.append("CONFIDENCE_BOUNDS")
    return problems


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    context = CaseContext(case=case, trace=trace)
    coordinator = Coordinator(gateway, context)
    decision = await coordinator.solve()
    output = build_output(context, decision)
    trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor=COORDINATOR,
        target=VERIFIER,
        decision_code="DRAFT_READY",
        evidence_refs=output["evidence_refs"][:20],
    )
    problems = verify(context, output)
    if problems:
        output["assessment"]["case_status"] = "needs_investigation"
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.4)
    trace.emit(
        case_id=context.case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        target=COORDINATOR,
        decision_code="FAIL" if problems else "PASS",
        evidence_refs=output["evidence_refs"][:20],
        attributes={"problems": ",".join(problems) or None, "primary_issue": decision["primary"]},
    )
    return output
