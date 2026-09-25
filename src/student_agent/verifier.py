"""Verifier agent: the last step of the L3A workflow.

The verifier never calls MCP tools. It receives specialist reports (A2A handoffs),
cross-checks their evidence, decides the primary issue, applies the case policy and
emits the final output that must satisfy ``l3a-output-v2.schema.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .contracts import Contracts
from .evidence import Evidence, EvidenceLedger
from .trace import TraceWriter

ORDER = "get_order"
ITEMS = "get_order_items"
PRODUCT = "get_product_context"
PAYMENTS = "get_order_payments"
PAYMENT_TIMELINE = "get_payment_timeline"
REFUNDS = "get_refund_timeline"
SHIPMENT = "get_shipment_summary"
SELLERS = "get_sellers"
POLICY = "get_policy"

INSUFFICIENT = "insufficient_evidence"
UNSUPPORTED = "unsupported_claim"
PRIMARY_ISSUES = frozenset(
    {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        UNSUPPORTED,
        INSUFFICIENT,
    }
)

# Evidence that actually supports each conclusion. Citing everything would hurt precision.
CITATIONS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": (ORDER, PAYMENT_TIMELINE, POLICY),
    "unavailable_order_paid": (ORDER, PAYMENT_TIMELINE, ITEMS, SELLERS, POLICY),
    "late_delivery_seller": (ORDER, SHIPMENT, ITEMS, SELLERS, POLICY),
    "late_delivery_logistics": (ORDER, SHIPMENT, POLICY),
    "valid_split_payment": (ORDER, PAYMENT_TIMELINE, ITEMS, POLICY),
    "payment_mismatch": (ORDER, PAYMENT_TIMELINE, POLICY),
    "duplicate_charge": (ORDER, PAYMENT_TIMELINE, ITEMS, POLICY),
    "refund_pending": (ORDER, REFUNDS, PAYMENT_TIMELINE, POLICY),
    "refund_failed": (ORDER, REFUNDS, PAYMENT_TIMELINE, POLICY),
    UNSUPPORTED: (ORDER, SHIPMENT, PAYMENT_TIMELINE, POLICY),
    INSUFFICIENT: (ORDER, POLICY),
}
# When the preferred tool is missing, cite the closest authoritative substitute.
SUBSTITUTES = {PAYMENT_TIMELINE: PAYMENTS, SHIPMENT: ORDER, SELLERS: ITEMS}

# Used only when the policy has no rule for the decided issue.
FALLBACK_RULES: dict[str, dict[str, Any]] = {
    INSUFFICIENT: {
        "case_status": "needs_investigation",
        "recommended_action": "escalate_manual_review",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "unknown", "party_id": None}],
    }
}

STRONG, MEDIUM = 0.95, 0.8
CENT = Decimal("0.01")


class VerificationError(RuntimeError):
    pass


@dataclass
class SpecialistReport:
    """A2A handoff payload from a specialist to the verifier."""

    agent: str
    case_id: str
    evidence: list[Evidence] = field(default_factory=list)
    proposed_issue: str | None = None
    confidence: float | None = None
    findings: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class Decision:
    issue: str
    confidence: float
    reason_code: str
    computed_refund: Decimal | None = None


@dataclass
class CaseFacts:
    order: dict[str, Any] | None
    items: list[dict[str, Any]]
    captures: list[dict[str, Any]]
    payment_events: list[dict[str, Any]]
    refunds: list[dict[str, Any]]
    shipment: dict[str, Any] | None
    shipment_events: list[dict[str, Any]]
    seller_ids: list[str]
    policy_rules: dict[str, Any]
    opened_at: datetime | None = None
    excluded_records: int = 0

    @property
    def order_total(self) -> Decimal:
        return sum(
            (_money(i.get("price")) + _money(i.get("freight_value")) for i in self.items),
            Decimal("0"),
        )

    @property
    def captured_total(self) -> Decimal:
        return sum((_money(c.get("amount_brl")) for c in self.captures), Decimal("0"))


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _dedupe(rows: list[Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = json.dumps(row, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def _same_day(value: Any, moment: datetime) -> bool:
    parsed = _ts(value)
    return parsed is not None and parsed.date() == moment.date()


def _within(value: Any, low: datetime | None, high: datetime | None) -> bool:
    moment = _ts(value)
    if moment is None:
        return False
    return (low is None or moment >= low) and (high is None or moment <= high)


def _unique(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(v for v in values if v not in (None, "")))


def scope_facts(case: dict[str, Any], data_by_tool: dict[str, Any]) -> CaseFacts:
    """Drop rows that fall outside this order's lifecycle (injected distractors).

    ``data_by_tool`` maps MCP tool names to their ``data`` payloads. Shared by the
    verifier and the specialists so every agent reasons over the same rows.
    """

    def data(tool: str) -> Any:
        return data_by_tool.get(tool)

    order = data(ORDER) if isinstance(data(ORDER), dict) else None
    opened = _ts(case.get("opened_at"))
    purchase = _ts(order.get("order_purchase_timestamp")) if order else None
    approved = (_ts(order.get("order_approved_at")) if order else None) or purchase
    estimated = _ts(order.get("order_estimated_delivery_date")) if order else None
    delivered = _ts(order.get("order_delivered_customer_date")) if order else None
    capture_end = approved + timedelta(days=1) if approved else opened
    tail_end = max((d for d in (opened, delivered) if d), default=None)
    if tail_end is not None:
        tail_end += timedelta(days=1)
    excluded = 0

    def scoped(rows: list[Any], key: str, low: Any, high: Any) -> list[dict[str, Any]]:
        nonlocal excluded
        rows = _dedupe(rows)
        if low is None and high is None:
            return rows
        kept = [r for r in rows if _within(r.get(key), low, high)]
        excluded += len(rows) - len(kept)
        return kept

    raw_items = data(ITEMS) if isinstance(data(ITEMS), list) else []
    items = scoped(raw_items, "shipping_limit_date", purchase, estimated or opened)
    if not items:
        items = _dedupe(raw_items)

    timeline = data(PAYMENT_TIMELINE) if isinstance(data(PAYMENT_TIMELINE), dict) else {}
    events = timeline.get("events") or []
    captures = scoped(
        [e for e in events if e.get("event_type") == "captured"], "event_at", purchase,
        capture_end,
    )
    payment_events = scoped(
        [e for e in events if e.get("event_type") != "captured"], "event_at", purchase,
        opened,
    )
    if not timeline and isinstance(data(PAYMENTS), list):
        captures = [
            {"amount_brl": p.get("payment_value"), "event_type": "captured"}
            for p in _dedupe(data(PAYMENTS))
        ]

    refund_data = data(REFUNDS) if isinstance(data(REFUNDS), dict) else {}
    refunds = scoped(refund_data.get("events") or [], "event_at", purchase, opened)

    shipment = data(SHIPMENT) if isinstance(data(SHIPMENT), dict) else None
    shipment_events = scoped(
        (shipment or {}).get("events") or [], "event_at", purchase, tail_end
    )

    seller_ids = _unique([i.get("seller_id") for i in items])
    if not seller_ids and isinstance(data(SELLERS), list):
        seller_ids = _unique([s.get("seller_id") for s in data(SELLERS)])

    policy = data(POLICY) if isinstance(data(POLICY), dict) else {}
    return CaseFacts(
        order=order,
        items=items,
        captures=captures,
        payment_events=payment_events,
        refunds=refunds,
        shipment=shipment,
        shipment_events=shipment_events,
        seller_ids=seller_ids,
        policy_rules=policy.get("rules") or {},
        opened_at=opened,
        excluded_records=excluded,
    )


def detect_payment_issue(facts: CaseFacts) -> Decision | None:
    """Money-side issues: refund lifecycle, paid-but-not-fulfilled, mismatch, duplicate, split."""
    order = facts.order or {}
    failed = [r for r in facts.refunds if r.get("status") == "failed"]
    if failed:
        amount = sum((_money(r.get("amount_brl")) for r in failed), Decimal("0"))
        return Decision("refund_failed", STRONG, "REFUND_FAILED", amount)
    if any(r.get("status") == "pending" for r in facts.refunds):
        # The refund is already in flight: monitor it, do not pay it twice.
        return Decision("refund_pending", STRONG, "REFUND_PENDING", Decimal("0"))

    status = order.get("order_status")
    if status in ("canceled", "unavailable") and facts.captures:
        return Decision(
            f"{status}_order_paid", STRONG, f"ORDER_{status.upper()}_WITH_CAPTURE",
            facts.captured_total,
        )

    mismatches = [
        e for e in facts.payment_events
        if e.get("event_type") == "reconciliation_mismatch" and e.get("status") != "resolved"
    ]
    if mismatches:
        amount = sum((_money(e.get("amount_brl")) for e in mismatches), Decimal("0"))
        return Decision("payment_mismatch", STRONG, "RECONCILIATION_MISMATCH_OPEN", amount)

    total = facts.order_total
    if len(facts.captures) >= 2:
        amounts = [_money(c.get("amount_brl")) for c in facts.captures]
        repeated = [a for a in set(amounts) if amounts.count(a) > 1]
        overpaid = total > 0 and facts.captured_total > total
        if repeated and overpaid:
            extra = sum((a * (amounts.count(a) - 1) for a in repeated), Decimal("0"))
            return Decision("duplicate_charge", STRONG, "REPEATED_CAPTURE_OVER_TOTAL", extra)
        if total > 0 and facts.captured_total == total:
            return Decision("valid_split_payment", STRONG, "SPLIT_CAPTURES_MATCH_TOTAL",
                            Decimal("0"))

    return None


def detect_late_delivery(facts: CaseFacts) -> Decision | None:
    """Lateness comes from the authoritative order timestamps, never from events alone."""
    order = facts.order or {}
    delivered = _ts(order.get("order_delivered_customer_date"))
    estimated = _ts(order.get("order_estimated_delivery_date"))
    if delivered is not None:
        is_late = estimated is not None and delivered > estimated
    else:
        # Not delivered yet: late only if the promise already expired when the case opened.
        opened = facts.opened_at
        is_late = (
            order.get("order_status") in ("shipped", "processing", "invoiced")
            and estimated is not None and opened is not None and opened > estimated
        )
    if not is_late:
        return None

    # A delivered_late event only attributes blame when it describes this delivery.
    late_events = [
        e for e in facts.shipment_events
        if e.get("event_type") == "delivered_late"
        and (delivered is None or _same_day(e.get("event_at"), delivered))
    ]
    carrier = _ts(order.get("order_delivered_carrier_date"))
    limits = [_ts(i.get("shipping_limit_date")) for i in facts.items]
    limits = [limit for limit in limits if limit]
    handoff_late = bool(carrier and limits and carrier > max(limits))
    actors = {e.get("actor") for e in late_events}

    if handoff_late or actors == {"seller"}:
        issue = "late_delivery_seller"
    else:
        issue = "late_delivery_logistics"
    corroborated = delivered is not None and bool(late_events) and (
        (issue == "late_delivery_seller") == ("seller" in actors)
    )
    freight = sum((_money(i.get("freight_value")) for i in facts.items), Decimal("0"))
    if facts.captured_total > 0:
        freight = min(freight, facts.captured_total)
    return Decision(
        issue,
        STRONG if corroborated else MEDIUM,
        "SELLER_HANDOFF_LATE" if issue == "late_delivery_seller" else "CARRIER_TRANSIT_LATE",
        freight,
    )


class VerifierAgent:
    actor = "verifier"

    def __init__(
        self, contracts: Contracts, ledger: EvidenceLedger, trace: TraceWriter
    ) -> None:
        self.contracts = contracts
        self.ledger = ledger
        self.trace = trace

    # ------------------------------------------------------------------ entry point
    def verify(self, case: dict[str, Any], reports: list[SpecialistReport]) -> dict[str, Any]:
        case_id = case["case_id"]
        evidence, rejected = self._accept_evidence(case_id, reports)
        facts = self._scope_facts(case, evidence)
        decision = self._decide(facts)
        decision, disagreements = self._reconcile(decision, facts, reports)

        rule = self._rule(facts, decision.issue)
        refund = self._refund(decision, rule)
        cited = self._cite(decision.issue, evidence)
        output = self._build_output(case, facts, decision, rule, refund, cited, evidence)
        violations = self._enforce_invariants(output, case_id, facts)

        self.contracts.validate_output(output, f"verifier output {case_id}")
        self._emit(case_id, output, rule, evidence, cited, len(reports), disagreements,
                   rejected, facts.excluded_records, violations)
        return output

    # ------------------------------------------------------------ evidence handling
    def _accept_evidence(
        self, case_id: str, reports: list[SpecialistReport]
    ) -> tuple[dict[str, Evidence], int]:
        """Keep only ledger-backed evidence of this case; first report wins per tool."""
        accepted: dict[str, Evidence] = {}
        rejected = 0
        for report in reports:
            if report.case_id != case_id:
                rejected += len(report.evidence)
                continue
            for item in report.evidence:
                if item.case_id != case_id or not self.ledger.owns(case_id, item.evidence_ref):
                    rejected += 1
                    continue
                accepted.setdefault(item.tool_name, item)
        return accepted, rejected

    def _scope_facts(self, case: dict[str, Any], evidence: dict[str, Evidence]) -> CaseFacts:
        return scope_facts(case, {tool: item.data for tool, item in evidence.items()})

    # ---------------------------------------------------------------- the decision
    def _decide(self, facts: CaseFacts) -> Decision:
        order = facts.order
        if order is None:
            return Decision(INSUFFICIENT, 0.3, "ORDER_EVIDENCE_MISSING")
        decision = detect_payment_issue(facts) or detect_late_delivery(facts)
        if decision is not None:
            return decision
        status = order.get("order_status")
        if status in ("canceled", "unavailable"):
            # Canceled/unavailable without an in-scope capture: nothing was paid.
            return Decision(UNSUPPORTED, MEDIUM, f"ORDER_{status.upper()}_NOT_CHARGED",
                            Decimal("0"))
        return Decision(UNSUPPORTED, 0.9, "NO_DEVIATION_FOUND", Decimal("0"))

    def _reconcile(
        self, decision: Decision, facts: CaseFacts, reports: list[SpecialistReport]
    ) -> tuple[Decision, int]:
        """Compare with specialist proposals; the evidence-backed decision always wins.

        Specialists see only their own domain and can be fooled by distractor rows
        (for example a stale delivered_late event), so a disagreement only lowers
        confidence. Without the order row there is nothing to adjudicate with.
        """
        proposals = [
            r for r in reports
            if r.proposed_issue in PRIMARY_ISSUES and r.proposed_issue != INSUFFICIENT
        ]
        disagreeing = [r for r in proposals if r.proposed_issue != decision.issue]
        if disagreeing and facts.order is not None:
            decision.confidence = max(0.5, decision.confidence - 0.05 * len(disagreeing))
        return decision, len(disagreeing)

    # ---------------------------------------------------------------- policy/refund
    def _rule(self, facts: CaseFacts, issue: str) -> dict[str, Any]:
        rule = facts.policy_rules.get(issue) or FALLBACK_RULES.get(issue)
        if rule is None:
            return dict(FALLBACK_RULES[INSUFFICIENT])
        return rule

    @staticmethod
    def _refund(decision: Decision, rule: dict[str, Any]) -> Decimal:
        if rule.get("case_status") == "no_action":
            return Decimal("0")
        if decision.computed_refund is not None and decision.computed_refund > 0:
            return decision.computed_refund
        return _money(rule.get("refund_brl", 0))

    def _cite(self, issue: str, evidence: dict[str, Evidence]) -> list[str]:
        refs: list[str] = []
        for tool in CITATIONS.get(issue, CITATIONS[INSUFFICIENT]):
            chosen = tool if tool in evidence else SUBSTITUTES.get(tool)
            if chosen in evidence:
                refs.append(evidence[chosen].evidence_ref)
        return _unique(refs)

    # ---------------------------------------------------------------------- output
    def _build_output(
        self,
        case: dict[str, Any],
        facts: CaseFacts,
        decision: Decision,
        rule: dict[str, Any],
        refund: Decimal,
        cited: list[str],
        evidence: dict[str, Evidence],
    ) -> dict[str, Any]:
        order = facts.order or {}
        order_id = order.get("order_id") or case.get("customer_request", {}).get(
            "claimed_order_id"
        )
        action = rule.get("recommended_action") or "escalate_manual_review"
        refund_lines = (
            [{"reason_code": action, "amount_brl": float(refund), "entity_id": order_id}]
            if refund > 0
            else []
        )
        return {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": case["case_id"],
            "assessment": {
                "primary_issue": decision.issue,
                "case_status": rule.get("case_status", "needs_investigation"),
                "confidence": round(decision.confidence, 2),
            },
            "affected_entities": {
                "order_ids": [order_id] if order and order_id else [],
                "item_ids": _unique([i.get("order_item_id") for i in facts.items])[:20],
                "seller_ids": facts.seller_ids[:20],
                "payment_references": self._explicit_ids(
                    facts.captures, ("payment_reference", "payment_id", "transaction_id")
                ),
                "shipment_ids": self._explicit_ids(
                    [facts.shipment or {}, *facts.shipment_events],
                    ("shipment_id", "tracking_id"),
                ),
            },
            "claim_assessments": self._claims(case, decision, action, cited, evidence),
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": decision.issue.upper(), "rank": 1}],
                "responsible_parties": self._parties(rule, facts),
            },
            "evidence_refs": cited,
            "data_conflicts": self._conflicts(facts, evidence),
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": float(refund),
                "refund_lines": refund_lines,
            },
            "resolution_actions": [action],
        }

    @staticmethod
    def _explicit_ids(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
        """Only ids that the evidence literally contains; never synthesize references."""
        return _unique([str(r[k]) for r in rows for k in keys if r.get(k)])[:20]

    @staticmethod
    def _parties(rule: dict[str, Any], facts: CaseFacts) -> list[dict[str, Any]]:
        parties: list[dict[str, Any]] = []
        for party in rule.get("responsible_parties") or []:
            if party.get("party_type") == "seller":
                # Policy ids are examples from other orders; bind to this order's seller.
                parties.extend(
                    {"party_type": "seller", "party_id": seller} for seller in facts.seller_ids
                )
                if not facts.seller_ids:
                    parties.append({"party_type": "seller", "party_id": None})
            else:
                parties.append(
                    {"party_type": party.get("party_type", "unknown"),
                     "party_id": party.get("party_id")}
                )
        return parties[:5] or [{"party_type": "unknown", "party_id": None}]

    def _claims(
        self,
        case: dict[str, Any],
        decision: Decision,
        action: str,
        cited: list[str],
        evidence: dict[str, Evidence],
    ) -> list[dict[str, Any]]:
        money_refs = _unique(
            [evidence[t].evidence_ref for t in (PAYMENT_TIMELINE, PAYMENTS, REFUNDS, POLICY)
             if t in evidence]
        )
        money_refs = [ref for ref in money_refs if ref in cited] or cited
        results: list[dict[str, Any]] = []
        for claim in (case.get("customer_request", {}).get("claims") or [])[:5]:
            topic = claim.get("topic")
            refs = cited
            if decision.issue == INSUFFICIENT:
                verdict = "insufficient_evidence"
            elif topic == "requested_full_refund":
                verdict = {
                    "issue_refund": "supported",
                    "document_no_action": "unsupported",
                    "monitor_refund": "unsupported",
                }.get(action, "partially_supported")
                refs = money_refs
            elif topic == UNSUPPORTED:
                verdict = (
                    "unsupported" if decision.issue == UNSUPPORTED else "insufficient_evidence"
                )
            elif topic in PRIMARY_ISSUES:
                verdict = "supported" if topic == decision.issue else "unsupported"
            else:
                verdict = "insufficient_evidence"
            results.append(
                {
                    "claim_id": str(claim.get("claim_id"))[:64],
                    "verdict": verdict,
                    "confidence": round(decision.confidence, 2),
                    "evidence_refs": refs,
                }
            )
        return results

    @staticmethod
    def _conflicts(facts: CaseFacts, evidence: dict[str, Evidence]) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        order, shipment = facts.order or {}, facts.shipment or {}
        pairs = (
            ("order_status", "order_status", "order_status"),
            ("delivered_customer_at", "order_delivered_customer_date", "delivered_customer_at"),
            ("delivered_carrier_at", "order_delivered_carrier_date", "delivered_carrier_at"),
            ("estimated_delivery_at", "order_estimated_delivery_date", "estimated_delivery_at"),
        )
        if order and shipment:
            for name, order_key, ship_key in pairs:
                if ship_key in shipment and order.get(order_key) != shipment.get(ship_key):
                    conflicts.append(
                        {
                            "field": name,
                            "sources": [ORDER, SHIPMENT],
                            "selected_source": ORDER,
                            "resolution_code": "PREFER_AUTHORITATIVE_ORDER_ROW",
                        }
                    )
        if PAYMENTS in evidence and PAYMENT_TIMELINE in evidence:
            rows = evidence[PAYMENTS].data if isinstance(evidence[PAYMENTS].data, list) else []
            timeline = evidence[PAYMENT_TIMELINE].data
            listed = timeline.get("payments") if isinstance(timeline, dict) else None
            if listed is not None and sorted(map(json.dumps, _dedupe(rows))) != sorted(
                map(json.dumps, _dedupe(listed))
            ):
                conflicts.append(
                    {
                        "field": "payments",
                        "sources": [PAYMENTS, PAYMENT_TIMELINE],
                        "selected_source": PAYMENT_TIMELINE,
                        "resolution_code": "PREFER_PAYMENT_LIFECYCLE_EVENTS",
                    }
                )
        return conflicts[:5]

    # ------------------------------------------------------------------ invariants
    def _enforce_invariants(
        self, output: dict[str, Any], case_id: str, facts: CaseFacts
    ) -> list[str]:
        """Repair what can be repaired deterministically and report every violation."""
        violations: list[str] = []
        assessment = output["assessment"]
        financial = output["financial_resolution"]

        owned = [ref for ref in output["evidence_refs"] if self.ledger.owns(case_id, ref)]
        if owned != output["evidence_refs"]:
            violations.append("UNOWNED_EVIDENCE_REF")
            output["evidence_refs"] = owned
        for claim in output.get("claim_assessments", []):
            claim["evidence_refs"] = [r for r in claim["evidence_refs"] if r in owned]

        if not owned and assessment["primary_issue"] != INSUFFICIENT:
            violations.append("NO_SUPPORTING_EVIDENCE")
            assessment.update(primary_issue=INSUFFICIENT, case_status="needs_investigation",
                              confidence=0.3)

        if assessment["case_status"] == "no_action" and financial["recommended_refund_brl"]:
            violations.append("NO_ACTION_WITH_REFUND")
            financial.update(recommended_refund_brl=0.0, refund_lines=[])

        line_total = sum(_money(line["amount_brl"]) for line in financial["refund_lines"])
        if line_total != _money(financial["recommended_refund_brl"]):
            violations.append("REFUND_LINES_MISMATCH")
            financial["recommended_refund_brl"] = float(line_total)

        paid = facts.captured_total
        if paid > 0 and _money(financial["recommended_refund_brl"]) > paid:
            violations.append("REFUND_EXCEEDS_CAPTURED")
            assessment["confidence"] = round(min(assessment["confidence"], MEDIUM), 2)

        sellers = set(output["affected_entities"]["seller_ids"])
        for party in output["root_cause_analysis"]["responsible_parties"]:
            if party["party_type"] == "seller" and party["party_id"] not in sellers:
                violations.append("SELLER_NOT_IN_AFFECTED_ENTITIES")
                if party["party_id"]:
                    output["affected_entities"]["seller_ids"].append(party["party_id"])
                    sellers.add(party["party_id"])

        assessment["confidence"] = min(1.0, max(0.0, float(assessment["confidence"])))
        return violations

    # ----------------------------------------------------------------------- trace
    def _emit(
        self,
        case_id: str,
        output: dict[str, Any],
        rule: dict[str, Any],
        evidence: dict[str, Evidence],
        cited: list[str],
        report_count: int,
        disagreements: int,
        rejected: int,
        excluded: int,
        violations: list[str],
    ) -> None:
        assessment = output["assessment"]
        if POLICY in evidence:
            self.trace.emit(
                case_id=case_id,
                event_type="policy_decided",
                actor=self.actor,
                tool_name=POLICY,
                decision_code=str(rule.get("recommended_action"))[:80],
                evidence_refs=[evidence[POLICY].evidence_ref],
                attributes={
                    "primary_issue": assessment["primary_issue"],
                    "case_status": assessment["case_status"],
                },
            )
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.actor,
            decision_code="PASS" if not violations else "PASS_WITH_REPAIRS",
            evidence_refs=output["evidence_refs"][:20] or None,
            attributes={
                "primary_issue": assessment["primary_issue"],
                "case_status": assessment["case_status"],
                "confidence": assessment["confidence"],
                "recommended_refund_brl": output["financial_resolution"]["recommended_refund_brl"],
                "reports_received": report_count,
                "specialist_disagreements": disagreements,
                "rejected_evidence": rejected,
                "excluded_out_of_scope_rows": excluded,
                "invariant_violations": ",".join(violations) or None,
            },
        )
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor,
            target="coordinator",
            decision_code="FINAL_OUTPUT_READY",
            evidence_refs=cited[:20] or None,
        )
