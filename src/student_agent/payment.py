from __future__ import annotations

import asyncio
import json
from typing import Any

from .llm import chat_completion
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verifier import detect_payment_issue, scope_facts

REFUND_REASON_CODES = {
    "duplicate_charge": "DUPLICATE_CHARGE_REFUND",
    "refund_failed": "RETRY_FAILED_REFUND",
    "canceled_order_paid": "CANCELED_ORDER_FULL_REFUND",
    "unavailable_order_paid": "UNAVAILABLE_ORDER_REFUND",
    "payment_mismatch": "RECONCILE_PAYMENT_MISMATCH",
}


async def call_tool_with_retry(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    max_retries: int = 3,
    **arguments: str,
) -> dict[str, Any] | None:
    """Gọi tool MCP an toàn với cơ chế retry (tối đa 3 lần theo ARCHITECTURE.md)."""
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            result = await gateway.call(tool_name, case_id=case_id, **arguments)
            return result
        except RuntimeError:
            # Lỗi cấp tool từ gateway (vd: order không có refund event) là tất định: không retry.
            return None
        except Exception as exc:
            error_str = str(exc).lower()
            if "not found" in error_str or "404" in error_str:
                return None
            if attempt == max_retries:
                return None
            await asyncio.sleep(delay)
            delay *= 2.0
    return None


async def analyze_payment_with_llm(
    customer_message: str,
    claim_topics: list[str],
    payments_list: list[dict[str, Any]],
    refund_data: dict[str, Any],
) -> dict[str, Any]:
    """Sử dụng Qwen 8B để hỗ trợ phân tích trong trường hợp khiếu nại thanh toán phức tạp."""
    prompt = f"""Bạn là chuyên gia thẩm định thanh toán thương mại điện tử.
Phân tích dữ liệu thanh toán sau và đưa ra đánh giá:

Khiếu nại khách hàng: "{customer_message}"
Chủ đề khiếu nại: {claim_topics}
Lịch sử thanh toán: {json.dumps(payments_list, ensure_ascii=False)}
Lịch sử hoàn tiền: {json.dumps(refund_data, ensure_ascii=False)}

Hãy trả về JSON theo định dạng:
{{
  "detected_issue": "duplicate_charge | payment_mismatch | refund_pending | refund_failed |
    valid_split_payment | canceled_order_paid | null",
  "reason_code": "Mã nguyên nhân viết HOA ví dụ DUPLICATE_CHARGE_REFUND",
  "refund_amount_brl": 0.0,
  "explanation": "Giải thích ngắn gọn 1 câu"
}}
"""
    try:
        response_text = await chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": "Bạn là chuyên gia đối soát tài chính, chỉ trả về JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            response_format_json=True,
            timeout=30.0,
        )
        parsed = json.loads(response_text)
        # LLM đôi khi trả về chuỗi/mảng JSON thay vì object: bỏ qua để không làm crash agent.
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def run_payment_agent(
    envelope: Any,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> Any:
    """Agent của Long: Chuyên gia điều tra Thanh toán & Dòng tiền.

    Nhiệm vụ:
    1. Xác định order_id từ payload.
    2. Gọi các MCP tool: get_order_payments, get_payment_timeline, get_refund_timeline.
    3. Thu thập evidence_ref và emit trace tool_result_consumed.
    4. Phân tích tài chính: duplicate_charge, payment_mismatch, refund_pending,
       refund_failed, valid_split_payment.
    5. Xây dựng cấu trúc financial_resolution chuẩn
       (currency BRL, recommended_refund_brl, refund_lines).
    6. Lưu payment_references và kết quả phân tích vào envelope để Verifier sử dụng.
    """
    case_id = envelope.case_id
    payload = envelope.payload or {}

    # 1. Trích xuất order_id từ payload
    order_id = (
        payload.get("order_id")
        or payload.get("claimed_order_id")
        or payload.get("customer_request", {}).get("claimed_order_id")
        or payload.get("order_data", {}).get("order_id")
        or ""
    )

    customer_req = payload.get("customer_request", {})
    customer_message = customer_req.get("message", "")
    claims = customer_req.get("claims", [])
    claim_topics = [c.get("topic", "") for c in claims if isinstance(c, dict)]

    # 2. Gọi các MCP tool để lấy bằng chứng xác thực
    payments_raw = None
    timeline_raw = None
    refund_raw = None

    if order_id:
        # Tool 1: get_order_payments
        payments_raw = await call_tool_with_retry(
            gateway, "get_order_payments", case_id=case_id, order_id=order_id
        )
        if payments_raw and "evidence_ref" in payments_raw:
            ref = payments_raw["evidence_ref"]
            if ref not in envelope.evidence_refs_collected:
                envelope.evidence_refs_collected.append(ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_order_payments",
                evidence_refs=[ref],
            )

        # Tool 2: get_payment_timeline
        timeline_raw = await call_tool_with_retry(
            gateway, "get_payment_timeline", case_id=case_id, order_id=order_id
        )
        if timeline_raw and "evidence_ref" in timeline_raw:
            ref = timeline_raw["evidence_ref"]
            if ref not in envelope.evidence_refs_collected:
                envelope.evidence_refs_collected.append(ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_payment_timeline",
                evidence_refs=[ref],
            )

        # Tool 3: get_refund_timeline
        refund_raw = await call_tool_with_retry(
            gateway, "get_refund_timeline", case_id=case_id, order_id=order_id
        )
        if refund_raw and "evidence_ref" in refund_raw:
            ref = refund_raw["evidence_ref"]
            if ref not in envelope.evidence_refs_collected:
                envelope.evidence_refs_collected.append(ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_agent",
                tool_name="get_refund_timeline",
                evidence_refs=[ref],
            )

    # 3. Trích xuất dữ liệu chi tiết
    payments_data = payments_raw.get("data", []) if payments_raw else []
    if isinstance(payments_data, dict):
        payments_list = payments_data.get("payments", []) or [payments_data]
    elif isinstance(payments_data, list):
        payments_list = payments_data
    else:
        payments_list = []

    refund_data = refund_raw.get("data", {}) if refund_raw else {}

    # 4. Phân tích dòng tiền & Payment References
    payment_references: list[str] = []
    total_paid_brl = 0.0
    payment_methods: list[str] = []

    for idx, p in enumerate(payments_list):
        if not isinstance(p, dict):
            continue
        val = float(p.get("payment_value", 0.0) or 0.0)
        total_paid_brl += val
        method = p.get("payment_type", "unknown")
        payment_methods.append(method)

        # Lấy payment_id hoặc định danh duy nhất (đảm bảo <= 128 chars theo schema)
        ref_id = str(
            p.get("payment_id")
            or p.get("payment_sequential")
            or f"{order_id}_pay_{idx + 1}"
        )[:128]
        if ref_id and ref_id not in payment_references:
            payment_references.append(ref_id)

    total_paid_brl = round(total_paid_brl, 2)

    # 5-6. Chỉ xét bản ghi thuộc vòng đời đơn hàng (dữ liệu có bản ghi gây nhiễu lệch thời gian),
    #      dùng chung bộ lọc và luật với Verifier để kết luận nhất quán.
    order_result = payload.get("order_agent_result") or {}
    facts = scope_facts(
        payload,
        {
            "get_order": order_result.get("order_data"),
            "get_order_items": order_result.get("items"),
            "get_order_payments": payments_list,
            "get_payment_timeline": timeline_raw.get("data") if timeline_raw else None,
            "get_refund_timeline": refund_data,
        },
    )
    latest_refund = facts.refunds[-1] if facts.refunds else {}
    refund_status = str(latest_refund.get("status", "none")).lower()
    refund_amount_brl = round(sum(float(r.get("amount_brl") or 0) for r in facts.refunds), 2)

    detected_issue: str | None = None
    recommended_refund_brl: float = 0.0
    refund_lines: list[dict[str, Any]] = []
    decision = detect_payment_issue(facts) if facts.order is not None else None
    if decision is not None:
        detected_issue = decision.issue
        recommended_refund_brl = float(decision.computed_refund or 0)
        if recommended_refund_brl > 0:
            refund_lines.append({
                "reason_code": REFUND_REASON_CODES.get(detected_issue, "ADJUSTMENT_REFUND"),
                "amount_brl": round(recommended_refund_brl, 2),
                "entity_id": order_id or None,
            })

    # e. Nếu chưa rõ và có claim về payment, hỏi Qwen 8B
    payment_related_claims = {
        "duplicate_charge", "payment_mismatch", "refund_pending", "refund_failed"
    }
    if not detected_issue and (set(claim_topics) & payment_related_claims):
        llm_res = await analyze_payment_with_llm(
            customer_message, claim_topics, payments_list, refund_data
        )
        if llm_res.get("detected_issue") in payment_related_claims:
            detected_issue = llm_res["detected_issue"]
            amt = float(llm_res.get("refund_amount_brl", 0.0) or 0.0)
            if amt > 0:
                recommended_refund_brl = amt
                refund_lines.append({
                    "reason_code": str(llm_res.get("reason_code", "PAYMENT_DISPUTE_REFUND"))[:80],
                    "amount_brl": round(amt, 2),
                    "entity_id": order_id or None,
                })

    # 7. Đảm bảo tính nhất quán tài chính (Financial Consistency Invariant)
    # Tổng refund_lines phải khớp với recommended_refund_brl
    if recommended_refund_brl > 0 and not refund_lines:
        refund_lines.append({
            "reason_code": "ADJUSTMENT_REFUND",
            "amount_brl": round(recommended_refund_brl, 2),
            "entity_id": order_id or None,
        })
    elif recommended_refund_brl == 0:
        refund_lines = []

    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": round(recommended_refund_brl, 2),
        "refund_lines": refund_lines,
    }

    # 8. Đóng gói kết quả gửi tiếp cho các Agent sau và Verifier
    envelope.payload["payment_analysis"] = {
        "order_id": order_id,
        "total_paid_brl": total_paid_brl,
        "payment_references": payment_references,
        "payment_methods": payment_methods,
        "refund_status": refund_status,
        "refund_amount_brl": refund_amount_brl,
        "detected_issue": detected_issue,
        "financial_resolution": financial_resolution,
        "responsible_party": {
            "party_type": "payment_provider"
            if detected_issue in ("duplicate_charge", "refund_failed")
            else "platform",
            "party_id": None,
        },
    }

    envelope.sender = "payment_agent"
    envelope.receiver = "shipment_policy_agent"
    return envelope
