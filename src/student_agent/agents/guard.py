"""Lớp bảo vệ cuối cùng trước khi trả output — chạy SAU verifier của TV5.

Owner: Thành viên 1 (feat/tv1-coordinator-trace)

Mục tiêu: không case nào dính hard gate hoặc lỗi consistency hiển nhiên, kể cả khi
specialist trả dữ liệu thiếu/sai. Mọi sửa đổi đều idempotent (chạy nhiều lần vẫn vậy).
"""

from __future__ import annotations

import copy
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .base import EvidenceLedger

SCHEMA_VERSION = "day09-l3a-output-v2"
ENTITY_KEYS = ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
CAUSE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
VERDICTS = {"supported", "unsupported", "partially_supported", "insufficient_evidence"}
PARTY_TYPES = {
    "seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown",
}


def money(value: Any) -> float:
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        return 0.0
    if not amount.is_finite() or amount < 0:
        return 0.0
    return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def refund_total(financial: dict[str, Any]) -> float:
    """Tổng hoàn tiền = tổng refund_lines (nguồn sự thật), không tin field tổng."""
    lines = [line for line in financial.get("refund_lines") or [] if isinstance(line, dict)]
    return money(sum(Decimal(str(money(line.get("amount_brl")))) for line in lines))


def _clamp(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(0.0, min(1.0, number)), 2)


def _id_set(values: Any, limit: int = 20) -> list[str]:
    ids = [str(v)[:128] for v in values or [] if v not in (None, "")]
    return list(dict.fromkeys(ids))[:limit]


def fallback_output(
    case: dict[str, Any], ledger: EvidenceLedger | None = None
) -> dict[str, Any]:
    """Output an toàn khi workflow lỗi: không kết luận, không bịa evidence."""
    refs = ledger.refs()[:30] if ledger else []
    claims = (case.get("customer_request") or {}).get("claims") or []
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {key: [] for key in ENTITY_KEYS},
        "claim_assessments": [
            {
                "claim_id": str(c["claim_id"])[:64],
                "verdict": "insufficient_evidence",
                "confidence": 0.2,
                "evidence_refs": refs,
            }
            for c in claims[:5]
            if c.get("claim_id")
        ],
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def enforce_invariants(
    output: dict[str, Any],
    case: dict[str, Any],
    ledger: EvidenceLedger,
) -> tuple[dict[str, Any], list[str]]:
    """Sửa output về trạng thái hợp lệ + nhất quán. Trả về (output, danh sách mã sửa)."""
    out = copy.deepcopy(output)
    fixes: list[str] = []
    case_id = case["case_id"]

    if out.get("case_id") != case_id:
        out["case_id"] = case_id
        fixes.append("case_id")
    out["schema_version"] = SCHEMA_VERSION

    # ── Assessment ────────────────────────────────────────────────────
    assessment = out.setdefault("assessment", {})
    assessment["confidence"] = _clamp(assessment.get("confidence"))
    issue = assessment.get("primary_issue")
    status = assessment.get("case_status")

    # ── Evidence: chỉ ref thật của case này ───────────────────────────
    refs = ledger.only_known(out.get("evidence_refs") or [])[:30]
    if refs != out.get("evidence_refs"):
        fixes.append("evidence_refs")
    out["evidence_refs"] = refs

    # ── Entities ─────────────────────────────────────────────────────
    entities = out.get("affected_entities") or {}
    out["affected_entities"] = {key: _id_set(entities.get(key)) for key in ENTITY_KEYS}

    # ── Claim assessments: chỉ claim có trong input ──────────────────
    input_claims = {
        c.get("claim_id") for c in (case.get("customer_request") or {}).get("claims") or []
    }
    claims_out = []
    for item in out.get("claim_assessments") or []:
        if item.get("claim_id") not in input_claims:
            fixes.append("claim_unknown")
            continue
        claims_out.append({
            "claim_id": item["claim_id"],
            "verdict": item.get("verdict") if item.get("verdict") in VERDICTS
            else "insufficient_evidence",
            "confidence": _clamp(item.get("confidence")),
            "evidence_refs": ledger.only_known(item.get("evidence_refs") or [])[:30],
        })
    out["claim_assessments"] = claims_out[:5]

    # ── Financial: Decimal, tổng khớp, không hoàn tiền nếu không cần hành động ──
    fin = out.get("financial_resolution") or {}
    lines = [
        {
            "reason_code": str(line.get("reason_code") or "refund")[:80],
            "amount_brl": money(line.get("amount_brl")),
            "entity_id": None if line.get("entity_id") is None
            else str(line["entity_id"])[:128],
        }
        for line in (fin.get("refund_lines") or [])[:10]
        if isinstance(line, dict)
    ]
    if status != "action_required" and lines:
        lines = []
        fixes.append("refund_without_action")
    total = money(sum(Decimal(str(line["amount_brl"])) for line in lines))
    if fin.get("recommended_refund_brl") != total:
        fixes.append("refund_total")
    out["financial_resolution"] = {
        "currency": "BRL",
        "recommended_refund_brl": total,
        "refund_lines": lines,
    }

    # ── Root cause: bên chịu trách nhiệm khớp issue ───────────────────
    rca = out.get("root_cause_analysis") or {}
    causes = [
        {"cause_code": c["cause_code"], "rank": c["rank"]}
        for c in rca.get("ranked_causes") or []
        if isinstance(c, dict)
        and CAUSE_CODE.match(str(c.get("cause_code", "")))
        and isinstance(c.get("rank"), int)
        and 1 <= c["rank"] <= 5
    ][:5]
    parties = [
        {"party_type": p["party_type"], "party_id": p.get("party_id")}
        for p in rca.get("responsible_parties") or []
        if isinstance(p, dict) and p.get("party_type") in PARTY_TYPES
    ]
    types = {p["party_type"] for p in parties}
    if issue == "late_delivery_seller" and "seller" not in types:
        sellers = out["affected_entities"]["seller_ids"] or [None]
        parties += [{"party_type": "seller", "party_id": sid} for sid in sellers]
        fixes.append("seller_party")
    if issue == "late_delivery_logistics" and "logistics_provider" not in types:
        parties.append({"party_type": "logistics_provider", "party_id": None})
        fixes.append("logistics_party")
    unique_parties = list({(p["party_type"], p["party_id"]): p for p in parties}.values())
    out["root_cause_analysis"] = {
        "ranked_causes": causes,
        "responsible_parties": unique_parties[:5],
    }

    # ── Conflicts & actions ───────────────────────────────────────────
    out["data_conflicts"] = [
        {
            "field": str(c["field"])[:100],
            "sources": list(dict.fromkeys(str(s)[:80] for s in c["sources"]))[:5],
            "selected_source": None if c.get("selected_source") is None
            else str(c["selected_source"])[:80],
            "resolution_code": str(c["resolution_code"])[:80],
        }
        for c in out.get("data_conflicts") or []
        if isinstance(c, dict)
        and c.get("field")
        and c.get("resolution_code")
        and len(set(c.get("sources") or [])) >= 2
    ][:5]
    actions = [str(a).strip()[:80] for a in out.get("resolution_actions") or []]
    out["resolution_actions"] = list(dict.fromkeys(a for a in actions if a))[:8]

    return out, fixes
