from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .. import OUTPUT_SCHEMA_VERSION
from ..trace import TraceWriter
from .policy_agent import PolicyAgent, PolicyDecision, clean_parties, to_money
from .state import CaseState

PRIMARY_ISSUES = (
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed",
    "unsupported_claim", "insufficient_evidence",
)
CLAIM_VERDICTS = ("supported", "unsupported", "partially_supported", "insufficient_evidence")
ENTITY_KEYS = ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
CAUSE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")

# Evidence domains that must be cited for each issue (hard gate: missing_required_evidence).
# "policy" is added on top whenever a policy rule decided the action.
REQUIRED_DOMAINS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order", "payment"),
    "unavailable_order_paid": ("order", "payment"),
    "late_delivery_seller": ("order", "shipment"),
    "late_delivery_logistics": ("order", "shipment"),
    "valid_split_payment": ("payment",),
    "payment_mismatch": ("payment",),
    "duplicate_charge": ("payment",),
    "refund_pending": ("refund",),
    "refund_failed": ("refund",),
    "unsupported_claim": (),
    "insufficient_evidence": (),
}
# Domains that may be cited per issue; other refs are dropped to protect evidence precision.
_ANY_CORE = frozenset({"order", "item", "payment", "shipment", "seller", "refund", "policy"})
RELEVANT_DOMAINS: dict[str, frozenset[str]] = {
    "canceled_order_paid": frozenset({"order", "payment", "refund", "policy"}),
    "unavailable_order_paid": frozenset({"order", "item", "seller", "payment", "refund", "policy"}),
    "late_delivery_seller": frozenset({"order", "item", "shipment", "seller", "policy"}),
    "late_delivery_logistics": frozenset({"order", "item", "shipment", "seller", "policy"}),
    "valid_split_payment": frozenset({"order", "payment", "policy"}),
    "payment_mismatch": frozenset({"order", "item", "payment", "policy"}),
    "duplicate_charge": frozenset({"order", "payment", "refund", "policy"}),
    "refund_pending": frozenset({"order", "payment", "refund", "policy"}),
    "refund_failed": frozenset({"order", "payment", "refund", "policy"}),
    "unsupported_claim": _ANY_CORE,
    "insufficient_evidence": _ANY_CORE,
}

MAX_EVIDENCE_REFS = 30
MAX_TRACE_REFS = 20
MIN_CONFIDENCE = 0.05
MAX_CONFIDENCE = 0.95
DEFAULT_CONFIDENCE = 0.7


class VerificationError(ValueError):
    pass


@dataclass
class VerificationReport:
    fixes: list[str] = field(default_factory=list)
    dropped_refs: int = 0
    missing_domains: list[str] = field(default_factory=list)

    @property
    def decision_code(self) -> str:
        if self.missing_domains:
            return "VERIFIED_MISSING_EVIDENCE"
        return "VERIFIED_WITH_FIXES" if self.fixes or self.dropped_refs else "VERIFIED"


def _text(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= max_length else None


def _unique_texts(values: Any, max_length: int, max_items: int) -> list[str]:
    result: list[str] = []
    for value in values if isinstance(values, list) else []:
        text = _text(value, max_length)
        if text and text not in result:
            result.append(text)
    return result[:max_items]


def _clamp_confidence(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        value = default
    return round(min(max(float(value), MIN_CONFIDENCE), MAX_CONFIDENCE), 2)


def _section(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class VerifierAgent:
    """Final gatekeeper: enforces hard gates, policy consistency, calibration and schema."""

    actor = "verifier"

    def __init__(self, policy_agent: PolicyAgent | None = None) -> None:
        self.policy_agent = policy_agent or PolicyAgent()

    def verify(self, state: CaseState, draft: dict[str, Any], trace: TraceWriter) -> dict[str, Any]:
        report = VerificationReport()
        if draft.get("case_id", state.case_id) != state.case_id:
            raise VerificationError(
                f"draft case_id {draft.get('case_id')!r} does not match {state.case_id}"
            )

        assessment = _section(draft.get("assessment"))
        primary_issue = assessment.get("primary_issue") or state.primary_issue_candidate
        if primary_issue not in PRIMARY_ISSUES:
            report.fixes.append("primary_issue_defaulted")
            primary_issue = "insufficient_evidence"

        decision = state.policy_decision
        if decision is None or decision.primary_issue != primary_issue:
            decision = self.policy_agent.decide(state, primary_issue, trace)

        entities = self._entities(state, draft, decision, report)
        evidence_refs = self._evidence_refs(state, draft, primary_issue, decision, report)
        confidence = self._confidence(
            state, assessment.get("confidence"), primary_issue, decision, evidence_refs, report
        )
        claims = self._claims(state, draft, primary_issue, evidence_refs, confidence, report)

        output: dict[str, Any] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": state.case_id,
            "assessment": {
                "primary_issue": primary_issue,
                "case_status": decision.case_status,
                "confidence": confidence,
            },
            "affected_entities": entities,
            "root_cause_analysis": self._root_cause(draft, decision, entities, report),
            "evidence_refs": evidence_refs,
            "data_conflicts": self._conflicts(state, draft, primary_issue, evidence_refs),
            "financial_resolution": self._financial(state, draft, decision, entities, report),
            "resolution_actions": self._actions(draft, decision, report),
        }
        if claims:
            output["claim_assessments"] = claims
        if assessment.get("case_status") not in (None, decision.case_status):
            report.fixes.append("case_status_aligned_to_policy")

        trace.contracts.validate_output(output, f"outputs/{state.case_id}.json")
        trace.emit(
            case_id=state.case_id,
            event_type="verification_completed",
            actor=self.actor,
            target="coordinator",
            decision_code=report.decision_code,
            evidence_refs=evidence_refs[:MAX_TRACE_REFS] or None,
            attributes={
                "primary_issue": primary_issue,
                "case_status": decision.case_status,
                "confidence": confidence,
                "fix_count": len(report.fixes),
                "fixes": ",".join(report.fixes) or None,
                "dropped_refs": report.dropped_refs,
                "missing_domains": ",".join(report.missing_domains) or None,
            },
        )
        return output

    def _entities(
        self,
        state: CaseState,
        draft: dict[str, Any],
        decision: PolicyDecision,
        report: VerificationReport,
    ) -> dict[str, list[str]]:
        raw = _section(draft.get("affected_entities"))
        entities = {key: _unique_texts(raw.get(key), 128, 20) for key in ENTITY_KEYS}
        claimed = _text(state.claimed_order_id, 128)
        # Only scope the claimed order in once MCP confirmed it exists for this case.
        if not entities["order_ids"] and claimed and state.refs_for_domain("order"):
            entities["order_ids"] = [claimed]
            report.fixes.append("order_id_from_evidence")
        # Only a seller that evidence ties to this case may be added; the shared policy's
        # example seller must never leak into the output.
        sellers = entities["seller_ids"]
        case_sellers = state.case_seller_ids()
        for party in decision.responsible_parties:
            seller_id = party["party_id"]
            if (
                party["party_type"] == "seller"
                and seller_id in case_sellers
                and seller_id not in sellers
                and len(sellers) < 20
            ):
                sellers.append(seller_id)
                report.fixes.append("responsible_seller_added")
        return entities

    def _evidence_refs(
        self,
        state: CaseState,
        draft: dict[str, Any],
        primary_issue: str,
        decision: PolicyDecision,
        report: VerificationReport,
    ) -> list[str]:
        allowed = RELEVANT_DOMAINS[primary_issue]
        required = REQUIRED_DOMAINS[primary_issue] + (("policy",) if decision.rule_found else ())
        refs: list[str] = []
        for ref in draft.get("evidence_refs") or []:
            record = state.evidence.get(ref) if isinstance(ref, str) else None
            # Unknown, cross-case or off-topic refs are hard-gate or precision risks: drop them.
            if record is None or record.case_id != state.case_id or record.domain not in allowed:
                report.dropped_refs += 1
            elif ref not in refs:
                refs.append(ref)
        keep: list[str] = []
        for domain in required:
            cited = [ref for ref in refs if state.evidence[ref].domain == domain]
            candidates = cited or state.refs_for_domain(domain)
            if not candidates:
                report.missing_domains.append(domain)
                continue
            if not cited:
                refs.append(candidates[0])
                report.fixes.append(f"added_{domain}_evidence")
            keep.append(candidates[0])
        # Trim to the schema limit without ever dropping the one ref per required domain.
        spare = MAX_EVIDENCE_REFS - len(keep)
        optional = [ref for ref in refs if ref not in keep][:spare]
        return [ref for ref in refs if ref in keep or ref in optional]

    def _confidence(
        self,
        state: CaseState,
        draft_confidence: Any,
        primary_issue: str,
        decision: PolicyDecision,
        evidence_refs: list[str],
        report: VerificationReport,
    ) -> float:
        value = _clamp_confidence(draft_confidence, DEFAULT_CONFIDENCE)
        if primary_issue == "insufficient_evidence":
            value = min(value, 0.6)
        elif not decision.rule_found:
            value -= 0.15
        value -= 0.2 * len(report.missing_domains)
        if any(state.evidence[ref].warnings for ref in evidence_refs):
            value -= 0.05
        if report.dropped_refs:
            value -= 0.05
        return _clamp_confidence(value, DEFAULT_CONFIDENCE)

    def _claims(
        self,
        state: CaseState,
        draft: dict[str, Any],
        primary_issue: str,
        evidence_refs: list[str],
        confidence: float,
        report: VerificationReport,
    ) -> list[dict[str, Any]]:
        input_claims = {claim["claim_id"]: claim for claim in state.claims}
        assessed: dict[str, dict[str, Any]] = {}
        for item in [*(draft.get("claim_assessments") or []), *state.claims_assessed]:
            if not isinstance(item, dict) or item.get("verdict") not in CLAIM_VERDICTS:
                continue
            claim_id = item.get("claim_id")
            if claim_id not in input_claims or claim_id in assessed:
                continue
            refs = [ref for ref in item.get("evidence_refs") or [] if ref in evidence_refs]
            verdict = item["verdict"]
            if verdict != "insufficient_evidence" and not refs:
                verdict = "insufficient_evidence"
                report.fixes.append("claim_without_evidence_downgraded")
            assessed[claim_id] = {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": _clamp_confidence(item.get("confidence"), confidence),
                "evidence_refs": refs,
            }

        # Fill only the deterministic cases the specialists left open.
        issue_refs = [
            ref for ref in evidence_refs
            if state.evidence[ref].domain in REQUIRED_DOMAINS[primary_issue]
        ] or [ref for ref in evidence_refs if state.evidence[ref].domain != "policy"]
        for claim_id, claim in input_claims.items():
            topic = claim.get("topic")
            if claim_id in assessed or topic not in PRIMARY_ISSUES or not issue_refs:
                continue
            if topic == primary_issue and primary_issue != "insufficient_evidence":
                verdict = "supported"
            elif primary_issue == "unsupported_claim":
                verdict = "unsupported"
            else:
                continue
            assessed[claim_id] = {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": issue_refs,
            }
            report.fixes.append("claim_assessment_added")
        return [assessed[claim_id] for claim_id in input_claims if claim_id in assessed][:5]

    def _conflicts(
        self,
        state: CaseState,
        draft: dict[str, Any],
        primary_issue: str,
        evidence_refs: list[str],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for item in [*state.conflicts_detected, *(draft.get("data_conflicts") or [])]:
            item = _section(item)
            field_name = _text(item.get("field"), 100)
            resolution = _text(item.get("resolution_code"), 80)
            sources = _unique_texts(item.get("sources"), 80, 5)
            selected = item.get("selected_source")
            if not field_name or not resolution or len(sources) < 2:
                continue
            conflict = {
                "field": field_name,
                "sources": sources,
                "selected_source": _text(selected, 80),
                "resolution_code": resolution,
            }
            if conflict not in conflicts:
                conflicts.append(conflict)

        if primary_issue != "insufficient_evidence":
            cited_domains = [state.evidence[ref].domain for ref in evidence_refs]
            source = next(
                (d for d in REQUIRED_DOMAINS[primary_issue] if d in cited_domains),
                next((d for d in cited_domains if d != "policy"), "mcp_evidence"),
            )
            claim_conflict = {
                "field": "assessment.primary_issue",
                "sources": ["customer_claim", source],
                "selected_source": source,
                "resolution_code": "AUTHORITATIVE_EVIDENCE_OVER_CLAIM",
            }
            contradicted = any(
                claim.get("topic") in PRIMARY_ISSUES and claim.get("topic") != primary_issue
                for claim in state.claims
            )
            known_fields = {conflict["field"] for conflict in conflicts}
            if contradicted and claim_conflict["field"] not in known_fields:
                conflicts.append(claim_conflict)
        return conflicts[:5]

    def _financial(
        self,
        state: CaseState,
        draft: dict[str, Any],
        decision: PolicyDecision,
        entities: dict[str, list[str]],
        report: VerificationReport,
    ) -> dict[str, Any]:
        candidate = _section(draft.get("financial_resolution")) or _section(
            state.financial_resolution_candidate
        )
        total = decision.refund_brl
        if to_money(candidate.get("recommended_refund_brl")) not in (None, total):
            report.fixes.append("refund_aligned_to_policy")

        lines: list[dict[str, Any]] = []
        if total > 0:
            for item in candidate.get("refund_lines") or []:
                item = _section(item)
                reason = _text(item.get("reason_code"), 80)
                amount = to_money(item.get("amount_brl"))
                entity_id = item.get("entity_id")
                if reason and amount is not None and amount > 0:
                    lines.append(
                        {
                            "reason_code": reason,
                            "amount_brl": amount,
                            "entity_id": _text(entity_id, 128),
                        }
                    )
            lines = lines[:10]
            exact = sum((line["amount_brl"] for line in lines), Decimal(0)) == total and sum(
                float(line["amount_brl"]) for line in lines
            ) == float(total)
            if not exact:
                if lines:
                    report.fixes.append("refund_lines_rebuilt")
                order_ids = entities["order_ids"]
                lines = [
                    {
                        "reason_code": decision.primary_issue,
                        "amount_brl": total,
                        "entity_id": order_ids[0] if order_ids else None,
                    }
                ]
        return {
            "currency": "BRL",
            "recommended_refund_brl": float(total),
            "refund_lines": [{**line, "amount_brl": float(line["amount_brl"])} for line in lines],
        }

    def _actions(
        self, draft: dict[str, Any], decision: PolicyDecision, report: VerificationReport
    ) -> list[str]:
        # The policy action is authoritative; extra free-text actions break status/action checks.
        actions = [decision.recommended_action]
        if _unique_texts(draft.get("resolution_actions"), 80, 8) not in ([], actions):
            report.fixes.append("actions_aligned_to_policy")
        return actions

    def _root_cause(
        self,
        draft: dict[str, Any],
        decision: PolicyDecision,
        entities: dict[str, list[str]],
        report: VerificationReport,
    ) -> dict[str, Any]:
        raw = _section(draft.get("root_cause_analysis"))
        ranked: list[tuple[int, str]] = []
        for item in raw.get("ranked_causes") or []:
            item = _section(item)
            code = re.sub(r"[^A-Z0-9_]", "_", str(item.get("cause_code", "")).strip().upper())
            rank = item.get("rank")
            if not isinstance(rank, int) or isinstance(rank, bool):
                rank = 5
            if CAUSE_CODE_PATTERN.fullmatch(code) and code not in [c for _, c in ranked]:
                ranked.append((rank, code))
        ranked.sort(key=lambda pair: pair[0])
        causes = [
            {"cause_code": code, "rank": index} for index, (_, code) in enumerate(ranked[:5], 1)
        ]
        if not causes:
            causes = [{"cause_code": decision.primary_issue.upper(), "rank": 1}]

        if decision.rule_found:
            parties = [dict(party) for party in decision.responsible_parties]
        else:
            parties = list(clean_parties(raw.get("responsible_parties"))) or [
                dict(party) for party in decision.responsible_parties
            ]
        sellers = entities["seller_ids"]
        checked: list[dict[str, Any]] = []
        for party in parties:
            if party["party_type"] == "seller":
                if party["party_id"] and party["party_id"] not in sellers:
                    party["party_id"] = None
                    report.fixes.append("unverified_seller_cleared")
                if not party["party_id"] and len(sellers) == 1:
                    party["party_id"] = sellers[0]
                    report.fixes.append("seller_party_id_filled")
            if party not in checked:
                checked.append(party)
        return {"ranked_causes": causes, "responsible_parties": checked}
