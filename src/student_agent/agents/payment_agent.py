"""Payment & Financial Resolution Specialist Agent.

Chuyên môn: Đối soát Giao dịch, Cổng thanh toán & Tính toán Đền bù Tài chính.

MCP Tools phụ trách:
  - get_order_payments   : Chi tiết các khoản thanh toán của đơn.
  - get_payment_timeline : Dòng thời gian xử lý giao dịch.
  - get_refund_timeline  : Tiến trình xử lý hoàn tiền.

Output L3A:
  - affected_entities.payment_references
  - financial_resolution  (currency, recommended_refund_brl, refund_lines)

DoD:
  - sum(line["amount_brl"] for line in refund_lines) == recommended_refund_brl
  - Không đề xuất hoàn tiền nếu giao dịch hợp lệ và đơn hoàn tất bình thường.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dec(val: Any) -> Decimal:
    """Convert any numeric value to a 2-decimal-place Decimal safely.

    Uses string formatting to bypass float binary representation artifacts
    (e.g. 0.1 + 0.2 != 0.3 in float arithmetic).
    """
    if val is None:
        return Decimal("0.00")
    if isinstance(val, Decimal):
        return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if isinstance(val, float):
        return Decimal(f"{val:.10f}").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _sum_dec(amounts: list[Decimal]) -> Decimal:
    total = Decimal("0.00")
    for a in amounts:
        total += a
    return total


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RefundLine:
    """Một dòng chi tiết đề xuất hoàn tiền."""
    reason_code: str          # slug mô tả lý do: 'duplicate_charge', 'overcharge_mismatch', v.v.
    amount_brl: Decimal       # Số tiền (Decimal, 2 chữ số thập phân)
    entity_id: str | None     # ID giao dịch, đơn hàng, refund-id …

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "amount_brl": float(self.amount_brl),
            "entity_id": self.entity_id,
        }


@dataclass
class PaymentInvestigationResult:
    """Kết quả trả về từ PaymentAgent, sẵn sàng để ghép vào L3A output."""

    # --- affected_entities.payment_references ---
    payment_references: list[str]

    # --- financial_resolution ---
    financial_resolution: dict[str, Any]

    # --- Classification ---
    detected_issue: str | None          # Một trong: valid_split_payment | payment_mismatch |
                                        # duplicate_charge | refund_pending | refund_failed | None
    # --- Evidence refs để ghép vào evidence_refs ---
    evidence_refs: list[str]

    # --- Số liệu bổ sung (hữu ích cho verifier / coordinator) ---
    captured_total_brl: float              # Tổng tiền đã trừ từ khách
    refunded_total_brl: float             # Tổng đã hoàn thành công
    refundable_total_brl: float           # Tổng còn lại có thể hoàn
    is_valid_transaction: bool            # True → không cần hành động


# ---------------------------------------------------------------------------
# Agent chính
# ---------------------------------------------------------------------------

class PaymentAgent:
    """Specialist Agent – Payment & Financial Resolution.

    Workflow nội bộ:
      1. Gọi ``get_order_payments``  → lấy danh sách khoản thanh toán, số tiền, loại thẻ.
      2. Gọi ``get_payment_timeline`` → kiểm tra timeline duyệt/từ chối, double-charge.
      3. Gọi ``get_refund_timeline``  → kiểm tra trạng thái hoàn tiền (pending/failed).
      4. Gọi ``_reconcile()``         → phân loại issue + tính financial_resolution.
    """

    ACTOR = "payment-agent"

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def investigate(
        self,
        *,
        case_id: str,
        order_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter | None = None,
        expected_order_total: float | int | Decimal | None = None,
        order_status: str | None = None,
        order_purchase_at: str | None = None,
        opened_at: str | None = None,
    ) -> PaymentInvestigationResult:
        """Gọi 3 MCP tools, đối soát dòng tiền và trả kết quả tài chính.

        Args:
            case_id:               ID case đang xử lý (bắt buộc truyền đúng cho MCP audit).
            order_id:              ID đơn hàng cần đối soát.
            gateway:               EvidenceGateway đang active.
            trace:                 TraceWriter (tuỳ chọn) để ghi audit log.
            expected_order_total:  Giá trị đơn hàng từ order-agent (dùng để đối chiếu mismatch).
            order_status:          Trạng thái đơn từ order-agent ('delivered', 'canceled', …).
            order_purchase_at:     Thời điểm mua hàng (lấy từ get_order) để lọc bản ghi nhiễu.
            opened_at:             Thời điểm mở case (lấy từ input case) để lọc bản ghi nhiễu.
        """
        evidence_refs: list[str] = []

        # Xây hàm lọc thời gian nếu có đủ thông tin
        in_window = _build_window(order_purchase_at, opened_at)

        # ── 1. get_order_payments ──────────────────────────────────────────
        pmts_ev = await gateway.call(
            "get_order_payments", case_id=case_id, order_id=order_id
        )
        pmts_ref = pmts_ev.get("evidence_ref", "")
        if pmts_ref:
            evidence_refs.append(pmts_ref)
            if trace:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.ACTOR,
                    tool_name="get_order_payments",
                    evidence_refs=[pmts_ref],
                )

        # ── 2. get_payment_timeline ────────────────────────────────────────
        ptl_ev = await gateway.call(
            "get_payment_timeline", case_id=case_id, order_id=order_id
        )
        ptl_ref = ptl_ev.get("evidence_ref", "")
        if ptl_ref:
            evidence_refs.append(ptl_ref)
            if trace:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.ACTOR,
                    tool_name="get_payment_timeline",
                    evidence_refs=[ptl_ref],
                )

        # ── 3. get_refund_timeline ─────────────────────────────────────────
        # Tool báo lỗi khi đơn không có refund (~60/100 case), nên phải bọc try/except
        try:
            rtl_ev = await gateway.call(
                "get_refund_timeline", case_id=case_id, order_id=order_id
            )
        except Exception:
            rtl_ev = {"data": {"events": []}}

        rtl_ref = rtl_ev.get("evidence_ref", "")
        if rtl_ref:
            evidence_refs.append(rtl_ref)
            if trace:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.ACTOR,
                    tool_name="get_refund_timeline",
                    evidence_refs=[rtl_ref],
                )

        # ── 4. Chuẩn hóa dữ liệu từ MCP response ─────────────────────────
        # get_order_payments trả list thẳng
        payments_raw = self._extract_list(pmts_ev.get("data", []), "payments")

        # get_payment_timeline trả dict với key "events"
        ptl_events = self._extract_list(ptl_ev.get("data"), "events")

        # get_refund_timeline trả dict với key "events"
        refund_events = self._extract_list(rtl_ev.get("data"), "events")

        # Lọc theo khoảng thời gian case để loại bỏ bản ghi nhiễu
        if in_window is not None:
            ptl_events = [e for e in ptl_events if in_window(e.get("event_at"))]
            refund_events = [e for e in refund_events if in_window(e.get("event_at"))]

        # ── 5. Đối soát ───────────────────────────────────────────────────
        return self._reconcile(
            case_id=case_id,
            order_id=order_id,
            payments=payments_raw,
            payment_events=ptl_events,
            refund_events=refund_events,
            evidence_refs=evidence_refs,
            expected_order_total=expected_order_total,
            order_status=order_status,
        )

    # ------------------------------------------------------------------
    # Reconciliation (tách riêng để dễ unit test)
    # ------------------------------------------------------------------

    def _reconcile(
        self,
        *,
        case_id: str,
        order_id: str,
        payments: list[dict[str, Any]],
        payment_timeline: list[dict[str, Any]] | None = None,
        refund_timeline: list[dict[str, Any]] | None = None,
        payment_events: list[dict[str, Any]] | None = None,
        refund_events: list[dict[str, Any]] | None = None,
        evidence_refs: list[str],
        expected_order_total: float | int | Decimal | None = None,
        order_status: str | None = None,
    ) -> PaymentInvestigationResult:
        """Thuần logic đối soát – không gọi gateway, không có side-effect.

        Hỗ trợ cả tên tham số cũ (payment_timeline/refund_timeline) và mới
        (payment_events/refund_events) để tương thích với tests hiện tại.
        """
        # Chuẩn hóa: ưu tiên tham số mới, fallback về cũ
        ptl_events = payment_events if payment_events is not None else (payment_timeline or [])
        rfnd_events = refund_events if refund_events is not None else (refund_timeline or [])

        # ── A. Tổng hợp thông tin thanh toán từ get_order_payments ──────────
        payment_references: list[str] = []
        captured_total = Decimal("0.00")

        for p in payments:
            ref = (
                p.get("payment_reference")
                or p.get("payment_id")
                or p.get("transaction_id")
            )
            if ref and str(ref) not in payment_references:
                payment_references.append(str(ref))

            # get_order_payments: trường tiền là payment_value
            amount = _dec(p.get("payment_value") or p.get("amount_brl") or p.get("amount") or 0)
            captured_total += amount

        # ── B. Phân tích payment timeline events ──────────────────────────
        # Dùng events "captured" từ get_payment_timeline (key "amount_brl")
        captured_events: list[dict[str, Any]] = [
            e for e in ptl_events if e.get("event_type") == "captured"
        ]
        has_mismatch_event = any(
            e.get("event_type") == "reconciliation_mismatch" for e in ptl_events
        )

        # Tính tổng captured từ timeline events
        captured_from_events = _sum_dec(
            [_dec(e.get("amount_brl") or e.get("amount") or 0) for e in captured_events]
        )

        # ── C. Phân tích refund events ─────────────────────────────────────
        pending_refunds:   list[dict[str, Any]] = []
        failed_refunds:    list[dict[str, Any]] = []
        completed_refunds: list[dict[str, Any]] = []
        refunded_total = Decimal("0.00")

        for r in rfnd_events:
            r_status = str(r.get("status", "")).lower()
            # get_refund_timeline: trường tiền là amount_brl
            r_amount = _dec(r.get("amount_brl") or r.get("amount") or r.get("refund_amount") or 0)
            r_id = r.get("refund_id") or r.get("id") or order_id

            if r_status in {"pending", "processing", "in_progress", "submitted"}:
                pending_refunds.append({"id": r_id, "amount": r_amount})
            elif r_status in {"failed", "error", "rejected", "declined"}:
                failed_refunds.append({"id": r_id, "amount": r_amount})
            elif r_status in {"completed", "success", "processed", "approved"}:
                completed_refunds.append({"id": r_id, "amount": r_amount})
                refunded_total += r_amount
            else:
                if r.get("is_completed"):
                    completed_refunds.append({"id": r_id, "amount": r_amount})
                    refunded_total += r_amount

        # ── D. Phát hiện duplicate charge từ timeline events ───────────────
        # Quy tắc: ≥ 2 lần "captured" cùng amount_brl, tổng > giá trị đơn → duplicate
        # Nếu tổng = giá trị đơn → valid_split_payment
        duplicate_items: list[dict[str, Any]] = []

        if len(captured_events) >= 2:
            # Nhóm các captured events theo amount_brl
            amount_counts: Counter[Decimal] = Counter()
            for evt in captured_events:
                amt = _dec(evt.get("amount_brl") or evt.get("amount") or 0)
                amount_counts[amt] += 1

            # Nếu có ≥ 2 event cùng amount và tổng > giá trị đơn → duplicate
            for amt, cnt in amount_counts.items():
                if cnt >= 2:
                    total_for_amt = amt * cnt
                    exp = (
                        _dec(expected_order_total)
                        if expected_order_total is not None
                        else None
                    )
                    # duplicate: tổng > expected, hoặc không biết expected
                    if exp is None or total_for_amt > exp:
                        # Lần đầu là hợp lệ, lần sau là duplicate
                        for _ in range(cnt - 1):
                            duplicate_items.append({"ref": order_id, "amount": amt})

        # Fallback: phát hiện duplicate từ get_order_payments (tương thích test cũ)
        if not duplicate_items and not captured_events:
            sig_map: dict[str, list[dict[str, Any]]] = {}
            for p in payments:
                p_type = str(p.get("payment_type", "")).lower()
                amount = _dec(p.get("payment_value") or p.get("amount_brl") or p.get("amount") or 0)
                ref = p.get("payment_reference") or p.get("payment_id") or order_id
                sig = f"{p_type}|{amount}"
                sig_map.setdefault(sig, []).append({"ref": ref, "amount": amount, "raw": p})

            for _, items in sig_map.items():
                if len(items) <= 1:
                    continue
                p_type = str(items[0]["raw"].get("payment_type", "")).lower()
                amount = items[0]["amount"]
                exp = _dec(expected_order_total) if expected_order_total is not None else None
                # credit_card trùng amount bằng đúng expected → rõ ràng là duplicate charge
                if p_type == "credit_card" and (exp is None or amount == exp):
                    for extra in items[1:]:
                        duplicate_items.append({"ref": extra["ref"], "amount": extra["amount"]})
                # Hoặc bất kỳ payment_type nào bị đánh dấu is_duplicate
                for item in items[1:]:
                    is_dup_flag = (
                        item["raw"].get("is_duplicate")
                        or item["raw"].get("status") == "duplicate"
                    )
                    if is_dup_flag and not any(
                        d["ref"] == item["ref"] for d in duplicate_items
                    ):
                        duplicate_items.append({"ref": item["ref"], "amount": item["amount"]})

        # Phát hiện duplicate từ payment timeline cũ (external_transaction_id)
        if not duplicate_items:
            seen_ext_ids: set[str] = set()
            for evt in ptl_events:
                evt_type = str(evt.get("event_type", evt.get("status", ""))).lower()
                if evt_type not in {"captured", "approved", "settled"}:
                    continue
                ext_id = str(
                    evt.get("external_transaction_id") or evt.get("transaction_id") or ""
                )
                if ext_id and ext_id in seen_ext_ids:
                    dup_amount = _dec(
                        evt.get("amount_brl") or evt.get("amount") or 0
                    )
                    ref_id = evt.get("payment_reference") or ext_id
                    duplicate_items.append({"ref": ref_id, "amount": dup_amount})
                if ext_id:
                    seen_ext_ids.add(ext_id)

        # ── E. Phân loại sự cố và xây refund_lines ─────────────────────────
        # Thứ tự ưu tiên: refund_failed > refund_pending > duplicate > mismatch > split > ok
        detected_issue: str | None = None
        refund_lines: list[RefundLine] = []

        if failed_refunds:
            detected_issue = "refund_failed"
            for fr in failed_refunds:
                refund_lines.append(RefundLine(
                    reason_code="refund_failed_retry",
                    amount_brl=fr["amount"],
                    entity_id=str(fr["id"]),
                ))

        elif pending_refunds:
            detected_issue = "refund_pending"
            for pr in pending_refunds:
                refund_lines.append(RefundLine(
                    reason_code="refund_pending_expedite",
                    amount_brl=pr["amount"],
                    entity_id=str(pr["id"]),
                ))

        elif duplicate_items:
            detected_issue = "duplicate_charge"
            for dup in duplicate_items:
                refund_lines.append(RefundLine(
                    reason_code="duplicate_charge",
                    amount_brl=dup["amount"],
                    entity_id=str(dup["ref"]) if dup["ref"] else order_id,
                ))

        elif has_mismatch_event:
            # Dùng event reconciliation_mismatch từ timeline thay vì so sánh captured_total
            detected_issue = "payment_mismatch"
            # Tính chênh lệch nếu có thể, fallback về captured từ events
            expected_total = (
                _dec(expected_order_total) if expected_order_total is not None else None
            )
            if expected_total is not None and captured_from_events > Decimal("0.00"):
                diff = captured_from_events - expected_total
                if diff > Decimal("0.00"):
                    refund_lines.append(RefundLine(
                        reason_code="overcharge_mismatch",
                        amount_brl=diff,
                        entity_id=order_id,
                    ))
            # Nếu không tính được chênh lệch, không thêm refund line (verifier sẽ xử lý)

        elif not has_mismatch_event and expected_order_total is not None:
            # Fallback: so captured_total (từ get_order_payments) vs expected
            expected_total = _dec(expected_order_total)
            diff = captured_total - expected_total
            if diff > Decimal("0.01"):
                detected_issue = "payment_mismatch"
                refund_lines.append(RefundLine(
                    reason_code="overcharge_mismatch",
                    amount_brl=diff,
                    entity_id=order_id,
                ))
            elif len(payments) > 1 and captured_total == expected_total or len(payments) > 1:
                detected_issue = "valid_split_payment"

        elif len(payments) > 1:
            detected_issue = "valid_split_payment"

        # ── F. Xử lý đơn bị hủy / không khả dụng ─────────────────────────
        if order_status in {"canceled", "unavailable"}:
            remaining = max(Decimal("0.00"), captured_total - refunded_total)
            if remaining > Decimal("0.00") and not refund_lines:
                reason = "order_canceled" if order_status == "canceled" else "order_unavailable"
                refund_lines.append(RefundLine(
                    reason_code=reason,
                    amount_brl=remaining,
                    entity_id=order_id,
                ))

        # ── G. Tính toán chính xác tổng hoàn tiền (Decimal, tránh float error) ──
        recommended_dec = _sum_dec([line.amount_brl for line in refund_lines])

        # ── H. Serialise refund_lines (float) ─────────────────────────────
        serialised_lines = [line.to_dict() for line in refund_lines]

        # ── I. DoD check (dùng raise thay vì assert để không bị tắt với -O) ──
        _check = _sum_dec([_dec(ln["amount_brl"]) for ln in serialised_lines])
        if _check != recommended_dec:
            raise ValueError(
                f"DoD violated: sum(refund_lines)={_check} != recommended={recommended_dec}"
            )

        # ── J. Xác định giao dịch có hợp lệ không ─────────────────────────
        is_valid = (
            detected_issue in {None, "valid_split_payment"}
            and recommended_dec == Decimal("0.00")
            and not failed_refunds
            and not pending_refunds
        )

        financial_resolution: dict[str, Any] = {
            "currency":               "BRL",
            "recommended_refund_brl": float(recommended_dec),
            "refund_lines":           serialised_lines,
        }

        refundable = max(Decimal("0.00"), captured_total - refunded_total)

        return PaymentInvestigationResult(
            payment_references    = payment_references[:20],   # schema maxItems: 20
            financial_resolution  = financial_resolution,
            detected_issue        = detected_issue,
            evidence_refs         = evidence_refs,
            captured_total_brl    = float(captured_total),
            refunded_total_brl    = float(refunded_total),
            refundable_total_brl  = float(refundable),
            is_valid_transaction  = is_valid,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_list(data: Any, key: str) -> list[dict[str, Any]]:
        """Chuẩn hóa data từ MCP: có thể là list thẳng hoặc wrapped trong dict."""
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            inner = data.get(key, data)
            if isinstance(inner, list):
                return [d for d in inner if isinstance(d, dict)]
            # dict đơn → đóng gói thành list
            return [data]
        return []


# ---------------------------------------------------------------------------
# Utility: xây hàm lọc thời gian
# ---------------------------------------------------------------------------

def _build_window(
    order_purchase_at: str | None,
    opened_at: str | None,
):
    """Trả về hàm in_window(value) hoặc None nếu thiếu thông tin."""
    if not order_purchase_at or not opened_at:
        return None
    try:
        from ..agents.evidence_rules import WINDOW_SLACK, parse_ts  # type: ignore[import]
        start = parse_ts(order_purchase_at)
        end = parse_ts(opened_at)
        if start is None or end is None:
            return None
        start = start - WINDOW_SLACK

        def in_window(value: str | None) -> bool:
            ts = parse_ts(value)
            return ts is not None and start <= ts <= end

        return in_window
    except Exception:
        return None
