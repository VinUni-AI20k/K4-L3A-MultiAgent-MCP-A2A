from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .a2a import AgentMessage, VerifiedFact
from .evidence import EvidenceCollector
from .observability import record_evidence_consumed
from .state import CaseState
from .trace import TraceWriter


async def inspect_policy(
    state: CaseState,
    collector: EvidenceCollector,
    trace: TraceWriter,
) -> AgentMessage:
    """Đọc policy đã xác minh; chưa quyết định kết quả case."""

    if collector.state is not state:
        raise ValueError("Collector and agent must share case state")

    evidence = await collector.collect(
        "policy-agent",
        "get_policy",
        policy_version=state.policy_version,
    )

    if evidence["domain"] != "policy":
        raise ValueError("Expected policy evidence")

    data = evidence["data"]
    if not isinstance(data, dict):
        raise ValueError("Policy data must be an object")

    if data.get("policy_version") != state.policy_version:
        raise ValueError("Policy version does not match case")

    if data.get("currency") != "BRL":
        raise ValueError("Unsupported policy currency")

    rules = data.get("rules")
    if not isinstance(rules, dict) or not rules:
        raise ValueError("Policy rules must be a non-empty object")

    party_types = {
        "seller",
        "platform",
        "logistics_provider",
        "payment_provider",
        "customer",
        "unknown",
    }

    for issue, rule in rules.items():
        if not isinstance(issue, str) or not issue.strip():
            raise ValueError("Invalid policy issue name")
        if not isinstance(rule, dict):
            raise ValueError("Policy rule must be an object")

        if rule.get("case_status") not in {
            "action_required", "no_action", "needs_investigation"
        }:
            raise ValueError("Invalid policy case_status")

        action = rule.get("recommended_action")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("Invalid recommended_action")

        try:
            amount = Decimal(str(rule.get("refund_brl")))
        except InvalidOperation as exc:
            raise ValueError("Invalid policy refund amount") from exc

        if not amount.is_finite() or amount < 0:
            raise ValueError("Policy refund must be finite and nonnegative")

        parties = rule.get("responsible_parties")
        if not isinstance(parties, list):
            raise ValueError("responsible_parties must be an array")

        for party in parties:
            if not isinstance(party, dict):
                raise ValueError("Responsible party must be an object")
            if party.get("party_type") not in party_types:
                raise ValueError("Invalid party_type")
            if "party_id" not in party:
                raise ValueError("Missing party_id")
            if party["party_id"] is not None and not isinstance(
                party["party_id"], str
            ):
                raise ValueError("party_id must be a string or null")

    evidence_ref = evidence["evidence_ref"]

    record_evidence_consumed(
        state, trace, "policy-agent", [evidence_ref]
    )

    return AgentMessage(
        case_id=state.case_id,
        sender="policy-agent",
        recipient="coordinator",
        task="Report validated policy rules",
        facts=[
            VerifiedFact(
                name="policy_rules",
                value=deepcopy(data),
                evidence_refs=[evidence_ref],
            )
        ],
        evidence_refs=[evidence_ref],
        status="completed",
    )