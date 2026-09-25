"""Policy agent: classify the primary issue from evidence-backed findings, then apply
the machine-readable policy returned by ``get_policy`` (status, action, parties).

RULES_VERSION identifies the rule/rounding/confidence set used in a run. Claim topics
are hypotheses only; they never decide the label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .a2a import (
    POLICY_AGENT,
    AgentContext,
    AgentResult,
    AgentTask,
    DataConflict,
    Finding,
    PolicyDecision,
    PolicyTaskPayload,
    SupportLink,
)
from .agents import money
from .ledger import EvidenceRecord, thaw
from .mcp_gateway import GatewayError, GatewayFatalError

RULES_VERSION = "l3a-rules-v2"
CENT = Decimal("0.01")
ZERO = Decimal("0")

# Fallback when get_policy is unavailable; mirrors the EC_POLICY_V1 rule shape.
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "responsible_parties": [{"party_type": "platform", "party_id": None}],
    },
    "unavailable_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "responsible_parties": [{"party_type": "seller", "party_id": "SELLER"}],
    },
    "late_delivery_seller": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "responsible_parties": [{"party_type": "seller", "party_id": "SELLER"}],
    },
    "late_delivery_logistics": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "responsible_parties": [{"party_type": "customer", "party_id": None}],
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "recommended_action": "reconcile_payment",
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "duplicate_charge": {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "refund_pending": {
        "case_status": "needs_investigation",
        "recommended_action": "monitor_refund",
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "refund_failed": {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
    },
    "unsupported_claim": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "responsible_parties": [{"party_type": "customer", "party_id": None}],
    },
    "insufficient_evidence": {
        "case_status": "needs_investigation",
        "recommended_action": "request_more_evidence",
        "responsible_parties": [{"party_type": "unknown", "party_id": None}],
    },
}

CAUSE_CODES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ITEM_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_LATE_HANDOFF",
    "late_delivery_logistics": "CARRIER_DELIVERY_DELAY",
    "valid_split_payment": "SPLIT_PAYMENT_MATCHES_ORDER_TOTAL",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_IN_PROGRESS",
    "refund_failed": "REFUND_PROCESSING_FAILED",
    "unsupported_claim": "NO_DISCREPANCY_IN_EVIDENCE",
    "insufficient_evidence": "EVIDENCE_UNAVAILABLE",
}

REFUND_REASON = {
    "canceled_order_paid": "CANCELED_ORDER_REFUND",
    "unavailable_order_paid": "UNAVAILABLE_ORDER_REFUND",
    "late_delivery_seller": "LATE_DELIVERY_FREIGHT_REFUND",
    "late_delivery_logistics": "LATE_DELIVERY_FREIGHT_REFUND",
    "payment_mismatch": "PAYMENT_MISMATCH_RECONCILIATION",
    "duplicate_charge": "DUPLICATE_CHARGE_REFUND",
    "refund_failed": "FAILED_REFUND_RETRY",
}


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class PolicyAgent:
    actor = POLICY_AGENT

    async def handle(self, task: AgentTask, context: AgentContext) -> AgentResult:
        payload = task.payload
        assert isinstance(payload, PolicyTaskPayload)
        policy_record = await self._policy(payload, context)
        decision, decision_code = decide(payload, policy_record)
        if (
            context.gateway is not None
            and policy_record is not None
            and not context.repair_reason_codes
            and policy_record.evidence_ref in decision.evidence_refs
        ):
            context.gateway.consume(self.actor, [policy_record])
        context.trace.emit(
            event_type="policy_decided",
            actor=self.actor,
            decision_code=decision_code,
            evidence_refs=list(decision.evidence_refs[:20]) or None,
            attributes={
                "task_id": task.task_id,
                "attempt": task.attempt,
                "rules_version": RULES_VERSION,
                "policy_version": payload.policy_version,
                "primary_issue": decision.assessment["primary_issue"],
                "case_status": decision.assessment["case_status"],
            },
        )
        return AgentResult(
            task.local_run_id, task.case_id, task.task_id, self.actor, "completed", decision
        )

    async def _policy(
        self, payload: PolicyTaskPayload, context: AgentContext
    ) -> EvidenceRecord | None:
        if not payload.policy_version or context.gateway is None:
            return None
        try:
            return await context.gateway.fetch(
                self.actor,
                "get_policy",
                deadline=context.deadline,
                policy_version=payload.policy_version,
            )
        except GatewayFatalError:
            raise
        except GatewayError:
            return None


@dataclass
class _Facts:
    status: Finding | None = None
    items: Finding | None = None
    payments: Finding | None = None
    refunds: Finding | None = None
    shipment: Finding | None = None
    sellers: list[str] = field(default_factory=list)
    order_id: str | None = None

    @classmethod
    def of(cls, findings: tuple[Finding, ...]) -> _Facts:
        facts = cls()
        for finding in findings:
            attr = {
                "ORDER_STATUS": "status",
                "ORDER_ITEMS": "items",
                "PAYMENTS": "payments",
                "REFUNDS": "refunds",
                "SHIPMENT": "shipment",
            }.get(finding.finding_code)
            if attr and getattr(facts, attr) is None:
                setattr(facts, attr, finding)
        if facts.items:
            facts.sellers = sorted(
                {line["seller_id"] for line in facts.items.value["lines"] if line.get("seller_id")}
            )
        if facts.status:
            facts.order_id = facts.status.value.get("order_id")
        return facts


def classify(facts: _Facts) -> tuple[str, Decimal, list[Finding], float]:
    """Return (primary_issue, refund amount, supporting findings, confidence)."""
    status = facts.status.value.get("status") if facts.status else None
    captures = facts.payments.value["captures"] if facts.payments else []
    pay_events = facts.payments.value["events"] if facts.payments else []
    paid = sum((Decimal(c["amount"]) for c in captures), ZERO)
    order_total = money(facts.items.value["order_total"]) if facts.items else None
    freight = money(facts.items.value["freight_total"]) if facts.items else None
    refunds = facts.refunds.value if facts.refunds else []
    shipment = facts.shipment.value if facts.shipment else None
    anchored = bool(facts.payments and facts.payments.value.get("anchored"))
    base_conf = 0.9 if anchored else 0.7

    def found(*items: Finding | None) -> list[Finding]:
        return [item for item in items if item is not None]

    if status in ("canceled", "unavailable") and paid > 0:
        issue = "canceled_order_paid" if status == "canceled" else "unavailable_order_paid"
        already = sum(
            (Decimal(r["amount"]) for r in refunds if r["status"] in ("completed", "succeeded")),
            ZERO,
        )
        return (
            issue,
            max(ZERO, paid - already),
            found(facts.status, facts.payments, facts.items, facts.refunds),
            base_conf,
        )
    failed = [r for r in refunds if r["status"] == "failed"]
    if failed:
        amount = sum((Decimal(r["amount"]) for r in failed), ZERO)
        return "refund_failed", amount, found(facts.refunds, facts.payments), base_conf
    pending = [r for r in refunds if r["status"] in ("pending", "processing", "requested")]
    if pending:
        return "refund_pending", ZERO, found(facts.refunds, facts.payments), base_conf
    mismatch = [e for e in pay_events if "mismatch" in e["event"]]
    if mismatch:
        amount = sum((Decimal(e["amount"]) for e in mismatch), ZERO)
        return "payment_mismatch", amount, found(facts.payments, facts.items), base_conf
    amounts = [Decimal(c["amount"]) for c in captures]
    duplicates = ZERO
    for value in set(amounts):
        if amounts.count(value) > 1:
            duplicates += value * (amounts.count(value) - 1)
    if duplicates > 0 and (order_total is None or paid - order_total > CENT):
        return "duplicate_charge", duplicates, found(facts.payments, facts.items), base_conf
    if shipment and (
        shipment["delivered_late"]
        or any(e["event"] == "delivered_late" for e in shipment["events"])
    ):
        actors = {e["actor"] for e in shipment["events"] if e["event"] == "delivered_late"}
        if "seller" in actors or (not actors and shipment["seller_handoff_late"]):
            issue = "late_delivery_seller"
        else:
            issue = "late_delivery_logistics"
        # Refund the freight actually paid: never more than freight or than captured.
        amount = freight or ZERO
        if paid > 0:
            amount = min(amount, paid) if amount > 0 else paid
        return issue, amount, found(facts.shipment, facts.items, facts.payments), base_conf - 0.05
    if len(captures) > 1 and order_total is not None and abs(paid - order_total) <= CENT:
        return "valid_split_payment", ZERO, found(facts.payments, facts.items), base_conf
    if facts.status and (facts.payments or facts.shipment):
        return "unsupported_claim", ZERO, found(facts.status, facts.payments, facts.shipment), 0.75
    return "insufficient_evidence", ZERO, found(facts.status, facts.payments), 0.6


def _rules(policy: EvidenceRecord | None) -> dict[str, dict[str, Any]]:
    if policy is None:
        return DEFAULT_RULES
    data = thaw(policy.data)
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, dict):
        return DEFAULT_RULES
    return {**DEFAULT_RULES, **rules}


def decide(payload: PolicyTaskPayload, policy: EvidenceRecord | None) -> tuple[PolicyDecision, str]:
    facts = _Facts.of(payload.findings)
    issue, amount, used, confidence = classify(facts)
    rule = _rules(policy)[issue]
    status = rule.get("case_status", DEFAULT_RULES[issue]["case_status"])
    action = rule.get("recommended_action")

    parties = []
    for party in rule.get("responsible_parties") or DEFAULT_RULES[issue]["responsible_parties"]:
        party_type = party.get("party_type", "unknown")
        if party_type == "seller":
            parties += [{"party_type": "seller", "party_id": s} for s in facts.sellers] or [
                {"party_type": "seller", "party_id": None}
            ]
        else:
            parties.append({"party_type": party_type, "party_id": None})

    refund = _round(amount) if status == "action_required" else ZERO
    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": REFUND_REASON.get(issue, issue.upper()),
                "amount_brl": refund,
                "entity_id": facts.order_id,
            }
        )
    actions = [action] if action else []
    if payload.conflicts:
        confidence = min(confidence, 0.7)

    refs = list(dict.fromkeys(ref for f in used for ref in f.evidence_refs))
    if policy is not None and refs:
        refs.append(policy.evidence_ref)

    claim_assessments = []
    for claim in payload.claims:
        verdict, claim_conf = _claim_verdict(claim.text, issue, action, refund)
        claim_assessments.append(
            {
                "claim_id": claim.claim_id[:64],
                "verdict": verdict,
                "confidence": claim_conf,
                "evidence_refs": refs[:30] if verdict != "insufficient_evidence" else [],
            }
        )

    decision = PolicyDecision(
        assessment={
            "primary_issue": issue,
            "case_status": status,
            "confidence": round(confidence, 2),
        },
        root_cause_analysis={
            "ranked_causes": [{"cause_code": CAUSE_CODES[issue], "rank": 1}],
            "responsible_parties": _unique_parties(parties),
        },
        financial_resolution={
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        resolution_actions=tuple(actions),
        data_conflicts=tuple(payload.conflicts[:5]),
        evidence_refs=tuple(refs[:30]),
        support_links=(
            SupportLink(
                "/assessment/primary_issue", tuple(f.finding_id for f in used), tuple(refs[:30])
            ),
        ),
        claim_assessments=tuple(claim_assessments),
    )
    return decision, issue.upper()


def _claim_verdict(
    topic: str, issue: str, action: str | None, refund: Decimal
) -> tuple[str, float]:
    if issue == "insufficient_evidence":
        return "insufficient_evidence", 0.6
    if topic == "requested_full_refund":
        if refund > 0 and action == "issue_refund":
            return "supported", 0.85
        if refund > 0:
            return "partially_supported", 0.75
        return "unsupported", 0.8
    if topic == issue:
        return "supported", 0.9
    return "unsupported", 0.85


def _unique_parties(parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, result = set(), []
    for party in parties:
        key = (party["party_type"], party["party_id"])
        if key not in seen:
            seen.add(key)
            result.append(party)
    return result[:5]


__all__ = ["RULES_VERSION", "DataConflict", "PolicyAgent", "decide"]
