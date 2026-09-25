"""Adjudicator — chọn primary_issue, case_status, confidence và evidence cuối cùng.

Owner: Thành viên 1 (feat/tv1-coordinator-trace)

Hàm thuần (không gọi MCP) để dễ test. Đầu vào là tín hiệu ``issues`` của các
specialist (xem ``base.py``) + ledger evidence của case.

Nguyên tắc:
- Claim của khách chỉ là GIẢ THUYẾT: tín hiệu khớp claim được cộng điểm nhỏ,
  nhưng không có tín hiệu từ evidence thì không kết luận.
- Evidence output = ref thuộc domain liên quan tới issue đã chọn + ref specialist
  gắn trực tiếp cho issue đó. Thừa một chút an toàn hơn thiếu (hard gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import PRIMARY_ISSUES, EvidenceLedger

# Domain evidence cần trích dẫn cho từng issue. Tinh chỉnh bằng điểm `evidence`
# trên public leaderboard — đây là giả thuyết khởi đầu, không phải oracle.
ISSUE_DOMAINS: dict[str, set[str]] = {
    "canceled_order_paid": {"order", "payment"},
    "unavailable_order_paid": {"order", "item", "payment"},
    "late_delivery_seller": {"order", "shipment", "seller"},
    "late_delivery_logistics": {"order", "shipment"},
    "valid_split_payment": {"order", "payment"},
    "payment_mismatch": {"order", "item", "payment"},
    "duplicate_charge": {"order", "payment"},
    "refund_pending": {"order", "payment", "refund"},
    "refund_failed": {"order", "payment", "refund"},
    "unsupported_claim": {"order"},
}
# Có quyết định hoàn tiền/hành động → trích policy làm căn cứ.
CITE_POLICY_WHEN_ACTION = True

DEFAULT_STATUS: dict[str, str] = {
    "valid_split_payment": "no_action",
    "unsupported_claim": "no_action",
    "insufficient_evidence": "needs_investigation",
}

# Thứ tự ưu tiên khi hai tín hiệu mạnh ngang nhau (issue "nặng" hơn đứng trước).
PRIORITY = (
    "duplicate_charge",
    "canceled_order_paid",
    "unavailable_order_paid",
    "refund_failed",
    "payment_mismatch",
    "refund_pending",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "unsupported_claim",
)

CLAIM_MATCH_BONUS = 0.10  # tín hiệu khớp giả thuyết của khách
CLOSE_MARGIN = 0.15  # hai ứng viên sát nhau → giảm confidence
MIN_STRENGTH = 0.30  # dưới ngưỡng này coi như không có tín hiệu
INSUFFICIENT_CONFIDENCE = 0.25


@dataclass
class Decision:
    primary_issue: str
    case_status: str
    confidence: float
    evidence_refs: list[str]
    candidates: list[tuple[str, float]] = field(default_factory=list)
    source: str = "signals"

    def assessment(self) -> dict[str, Any]:
        return {
            "primary_issue": self.primary_issue,
            "case_status": self.case_status,
            "confidence": self.confidence,
        }


def claimed_topics(claims: list[dict[str, Any]]) -> list[str]:
    return [c.get("topic", "") for c in claims if c.get("topic") in PRIMARY_ISSUES]


def collect_signals(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Gom tín hiệu hợp lệ từ mọi specialist; bỏ qua phần tử sai format."""
    signals: list[dict[str, Any]] = []
    for agent, result in results.items():
        for raw in result.get("issues") or []:
            if not isinstance(raw, dict) or raw.get("issue") not in PRIMARY_ISSUES:
                continue
            try:
                strength = float(raw.get("strength", 0.0))
            except (TypeError, ValueError):
                continue
            signals.append({**raw, "strength": max(0.0, min(1.0, strength)), "agent": agent})
    return signals


def select_evidence(
    issue: str,
    case_status: str,
    ledger: EvidenceLedger,
    direct_refs: list[str],
) -> list[str]:
    if issue == "insufficient_evidence":
        # Không kết luận được → trích những gì đã thực sự tra cứu.
        return ledger.refs()[:30]
    domains = set(ISSUE_DOMAINS.get(issue, {"order"}))
    if CITE_POLICY_WHEN_ACTION and case_status == "action_required":
        domains.add("policy")
    selected = ledger.only_known(direct_refs + ledger.refs(domains))
    return selected[:30]


def decide(
    claims: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    ledger: EvidenceLedger,
    fallback_assessment: dict[str, Any] | None = None,
) -> Decision:
    """Chọn kết luận cuối cùng cho case."""
    hypotheses = set(claimed_topics(claims))
    signals = [s for s in collect_signals(results) if s["strength"] >= MIN_STRENGTH]

    # Điểm mỗi issue = tín hiệu mạnh nhất (+ bonus nếu khớp giả thuyết khách).
    best: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    direct: dict[str, list[str]] = {}
    for sig in signals:
        issue = sig["issue"]
        if issue == "insufficient_evidence":
            continue
        score = sig["strength"] + (CLAIM_MATCH_BONUS if issue in hypotheses else 0.0)
        direct.setdefault(issue, []).extend(sig.get("evidence_refs") or [])
        if score > scores.get(issue, -1.0):
            scores[issue] = score
            best[issue] = sig

    if not scores:
        return _fallback(fallback_assessment, ledger)

    rank = {issue: i for i, issue in enumerate(PRIORITY)}
    candidates = sorted(scores.items(), key=lambda kv: (-kv[1], rank.get(kv[0], 99)))
    issue, score = candidates[0]
    winner = best[issue]
    status = winner.get("case_status") or DEFAULT_STATUS.get(issue, "action_required")

    confidence = min(score, 0.95)
    if len(candidates) > 1 and score - candidates[1][1] < CLOSE_MARGIN:
        confidence -= 0.15
    if hypotheses and issue not in hypotheses and issue != "unsupported_claim":
        confidence -= 0.05  # evidence chỉ ra vấn đề khác với lời khai
    confidence = round(max(0.05, min(0.95, confidence)), 2)

    return Decision(
        primary_issue=issue,
        case_status=status,
        confidence=confidence,
        evidence_refs=select_evidence(issue, status, ledger, direct.get(issue, [])),
        candidates=[(i, round(s, 3)) for i, s in candidates],
    )


def _fallback(assessment: dict[str, Any] | None, ledger: EvidenceLedger) -> Decision:
    """Không có tín hiệu specialist → dùng assessment của policy agent nếu có, không thì
    ``insufficient_evidence``. Không bao giờ đoán từ lời khai."""
    issue = (assessment or {}).get("primary_issue")
    if issue in PRIMARY_ISSUES and issue != "insufficient_evidence" and ledger.records:
        status = assessment.get("case_status") or DEFAULT_STATUS.get(issue, "action_required")
        try:
            confidence = float(assessment.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return Decision(
            primary_issue=issue,
            case_status=status,
            confidence=round(max(0.05, min(0.95, confidence)), 2),
            evidence_refs=select_evidence(issue, status, ledger, []),
            source="policy_agent",
        )
    return Decision(
        primary_issue="insufficient_evidence",
        case_status="needs_investigation",
        confidence=INSUFFICIENT_CONFIDENCE,
        evidence_refs=select_evidence("insufficient_evidence", "needs_investigation", ledger, []),
        source="no_signal",
    )


def default_claim_assessments(
    claims: list[dict[str, Any]],
    decision: Decision,
    refund_brl: float,
) -> list[dict[str, Any]]:
    """Claim assessment mặc định suy ra từ quyết định cuối — TV2 có thể ghi đè bằng
    key ``claim_assessments`` trong kết quả của order agent."""
    out: list[dict[str, Any]] = []
    issue, refs = decision.primary_issue, decision.evidence_refs
    for claim in claims[:5]:
        topic = claim.get("topic", "")
        if issue == "insufficient_evidence":
            verdict, conf = "insufficient_evidence", decision.confidence
        elif topic == "requested_full_refund":
            if decision.case_status != "action_required" or refund_brl <= 0:
                verdict = "unsupported"
            else:
                verdict = "supported"
            conf = decision.confidence
        elif topic == issue:
            verdict, conf = "supported", decision.confidence
        else:
            verdict, conf = "unsupported", decision.confidence
        out.append({
            "claim_id": claim["claim_id"],
            "verdict": verdict,
            "confidence": conf,
            "evidence_refs": list(refs),
        })
    return out
