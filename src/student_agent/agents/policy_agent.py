from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .state import CaseState

POLICY_TOOL = "get_policy"
CASE_STATUSES = ("action_required", "no_action", "needs_investigation")
PARTY_TYPES = (
    "seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"
)
CENT = Decimal("0.01")


def to_money(value: Any) -> Decimal | None:
    """Parse a BRL amount into a non-negative Decimal rounded to cents, or None if invalid."""
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def clean_parties(raw: Any) -> tuple[dict[str, str | None], ...]:
    """Keep schema-valid responsible parties, deduplicated, in order, at most 5."""
    parties: list[dict[str, str | None]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or item.get("party_type") not in PARTY_TYPES:
            continue
        raw_id = item.get("party_id")
        party_id = (raw_id.strip()[:128] or None) if isinstance(raw_id, str) else None
        party = {"party_type": item["party_type"], "party_id": party_id}
        if party not in parties:
            parties.append(party)
    return tuple(parties[:5])


@dataclass(frozen=True)
class PolicyDecision:
    primary_issue: str
    rule_found: bool
    case_status: str
    recommended_action: str
    refund_brl: Decimal
    responsible_parties: tuple[dict[str, str | None], ...]
    evidence_ref: str | None


class PolicyAgent:
    """Fetches the case's platform policy and maps a primary issue to the policy rule."""

    actor = "policy-agent"

    async def load(
        self, state: CaseState, gateway: EvidenceGateway, trace: TraceWriter
    ) -> dict[str, Any]:
        if state.policy_data is not None:
            return state.policy_data
        evidence = await state.fetch(
            gateway,
            trace,
            tool_name=POLICY_TOOL,
            actor=self.actor,
            policy_version=state.policy_version,
        )
        data = evidence.get("data")
        state.policy_data = data if isinstance(data, dict) else {}
        return state.policy_data

    def lookup(self, state: CaseState, primary_issue: str) -> PolicyDecision:
        """Pure rule lookup; falls back to a no-refund investigation when no valid rule exists."""
        rules = (state.policy_data or {}).get("rules")
        rule = rules.get(primary_issue) if isinstance(rules, dict) else None
        policy_refs = state.refs_for_domain("policy")
        if isinstance(rule, dict):
            status = rule.get("case_status")
            action = rule.get("recommended_action")
            refund = to_money(rule.get("refund_brl", 0))
            if (
                status in CASE_STATUSES
                and isinstance(action, str)
                and 0 < len(action.strip()) <= 80
                and refund is not None
            ):
                return PolicyDecision(
                    primary_issue=primary_issue,
                    rule_found=True,
                    case_status=status,
                    recommended_action=action.strip(),
                    refund_brl=Decimal(0) if status == "no_action" else refund,
                    responsible_parties=clean_parties(rule.get("responsible_parties")),
                    evidence_ref=policy_refs[0] if policy_refs else None,
                )
        action = (
            "request_additional_evidence"
            if primary_issue == "insufficient_evidence"
            else "escalate_manual_review"
        )
        return PolicyDecision(
            primary_issue=primary_issue,
            rule_found=False,
            case_status="needs_investigation",
            recommended_action=action,
            refund_brl=Decimal(0),
            responsible_parties=({"party_type": "unknown", "party_id": None},),
            evidence_ref=None,
        )

    def decide(self, state: CaseState, primary_issue: str, trace: TraceWriter) -> PolicyDecision:
        decision = self.lookup(state, primary_issue)
        state.policy_decision = decision
        trace.emit(
            case_id=state.case_id,
            event_type="policy_decided",
            actor=self.actor,
            decision_code=decision.recommended_action,
            evidence_refs=[decision.evidence_ref] if decision.evidence_ref else None,
            attributes={
                "primary_issue": primary_issue,
                "case_status": decision.case_status,
                "refund_brl": float(decision.refund_brl),
                "rule_found": decision.rule_found,
                "policy_version": state.policy_version,
            },
        )
        return decision
