"""Evidence rules — suy ra tín hiệu issue từ dữ liệu MCP thật của case.

Owner: Thành viên 1 (feat/tv1-coordinator-trace)

Quan sát trên dữ liệu thật: mỗi tool trả cả bản ghi của case LẪN bản ghi "nhiễu" nằm
ngoài dòng thời gian của case (trước ngày mua hoặc sau ``opened_at``) — vd refund
``failed`` từ tháng trước, sự kiện ``delivered_late`` sau khi case đã mở. Chỉ bản ghi
trong cửa sổ ``[order_purchase_timestamp - 1 ngày, opened_at]`` được dùng để kết luận;
bản ghi ngoài cửa sổ được báo cáo thành ``data_conflicts``.

Hàm thuần: đọc ``EvidenceLedger`` (đã lưu ``data`` của mỗi tool), không gọi MCP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .base import BaseAgent, EvidenceLedger

CENT = Decimal("0.01")
WINDOW_SLACK = timedelta(days=1)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def amount(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _rows(data: Any, key: str | None = None) -> list[dict[str, Any]]:
    if key and isinstance(data, dict):
        data = data.get(key)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


@dataclass
class CaseFacts:
    """Sự thật đã lọc theo cửa sổ thời gian của case."""

    order_id: str | None = None
    order_status: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    order_total: Decimal | None = None
    captured: list[dict[str, Any]] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    delivered_carrier_at: datetime | None = None
    delivered_customer_at: datetime | None = None
    estimated_delivery_at: datetime | None = None
    shipping_limit_at: datetime | None = None
    late_actors: list[str] = field(default_factory=list)
    stale: dict[str, int] = field(default_factory=dict)  # tool -> số bản ghi ngoài cửa sổ
    refs: dict[str, str] = field(default_factory=dict)  # tool -> evidence_ref

    def in_window(self, ts: datetime | None) -> bool:
        if ts is None or self.window_end is None:
            return False
        start = self.window_start - WINDOW_SLACK if self.window_start else None
        return (start is None or ts >= start) and ts <= self.window_end

    def ref(self, *tools: str) -> list[str]:
        return [self.refs[t] for t in tools if t in self.refs]


def extract_facts(case: dict[str, Any], ledger: EvidenceLedger) -> CaseFacts:
    facts = CaseFacts(window_end=parse_ts(case.get("opened_at")))
    by_tool = ledger.by_tool()
    facts.refs = {tool: rec.ref for tool, rec in by_tool.items()}

    def stale(tool: str, count: int) -> None:
        if count:
            facts.stale[tool] = facts.stale.get(tool, 0) + count

    order = by_tool.get("get_order")
    if order and isinstance(order.data, dict):
        facts.order_id = order.data.get("order_id")
        facts.order_status = order.data.get("order_status")
        facts.window_start = parse_ts(order.data.get("order_purchase_timestamp"))

    # ── Items: giữ dòng có shipping_limit trong cửa sổ ───────────────
    if "get_order_items" in by_tool:
        rows = _rows(by_tool["get_order_items"].data)
        kept = [r for r in rows if facts.in_window(parse_ts(r.get("shipping_limit_date")))]
        stale("get_order_items", len(rows) - len(kept))
        facts.items = kept
        total = Decimal(0)
        for row in kept:
            total += (amount(row.get("price")) or 0) + (amount(row.get("freight_value")) or 0)
        facts.order_total = total if kept else None

    # ── Payment timeline: sự kiện trong cửa sổ ───────────────────────
    if "get_payment_timeline" in by_tool:
        events = _rows(by_tool["get_payment_timeline"].data, "events")
        kept = [e for e in events if facts.in_window(parse_ts(e.get("event_at")))]
        stale("get_payment_timeline", len(events) - len(kept))
        facts.captured = [e for e in kept if e.get("event_type") == "captured"]
        facts.mismatches = [e for e in kept if "mismatch" in str(e.get("event_type", ""))]

    # ── Refund timeline ──────────────────────────────────────────────
    if "get_refund_timeline" in by_tool:
        events = _rows(by_tool["get_refund_timeline"].data, "events")
        kept = [e for e in events if facts.in_window(parse_ts(e.get("event_at")))]
        stale("get_refund_timeline", len(events) - len(kept))
        facts.refunds = sorted(kept, key=lambda e: e.get("event_at", ""))

    # ── Shipment summary ─────────────────────────────────────────────
    ship = by_tool.get("get_shipment_summary")
    if ship and isinstance(ship.data, dict):
        data = ship.data
        facts.order_status = facts.order_status or data.get("order_status")
        facts.delivered_carrier_at = parse_ts(data.get("delivered_carrier_at"))
        facts.delivered_customer_at = parse_ts(data.get("delivered_customer_at"))
        facts.estimated_delivery_at = parse_ts(data.get("estimated_delivery_at"))
        limits = _rows(data, "shipping_limits")
        kept = [lim for lim in limits if facts.in_window(parse_ts(lim.get("shipping_limit_at")))]
        stale("get_shipment_summary", len(limits) - len(kept))
        deadlines = [parse_ts(lim.get("shipping_limit_at")) for lim in kept]
        facts.shipping_limit_at = min((d for d in deadlines if d), default=None)
        events = _rows(data, "events")
        late = [e for e in events if e.get("event_type") == "delivered_late"]
        in_late = [e for e in late if facts.in_window(parse_ts(e.get("event_at")))]
        stale("get_shipment_summary", len(late) - len(in_late))
        facts.late_actors = [str(e.get("actor")) for e in in_late]
        for lim in kept:
            if lim.get("seller_id"):
                facts.seller_ids.append(str(lim["seller_id"]))

    # Nguồn fallback khi thiếu shipping_limits: deadline + seller từ items.
    if facts.shipping_limit_at is None and facts.items:
        deadlines = [parse_ts(r.get("shipping_limit_date")) for r in facts.items]
        facts.shipping_limit_at = min((d for d in deadlines if d), default=None)
    for row in facts.items:
        if row.get("seller_id"):
            facts.seller_ids.append(str(row["seller_id"]))
        if row.get("order_item_id"):
            facts.item_ids.append(str(row["order_item_id"]))
    facts.seller_ids = list(dict.fromkeys(facts.seller_ids))
    facts.item_ids = list(dict.fromkeys(facts.item_ids))
    return facts


def _captured_amounts(facts: CaseFacts) -> list[Decimal]:
    return [a for a in (amount(e.get("amount_brl")) for e in facts.captured) if a is not None]


def derive_signals(facts: CaseFacts) -> list[dict[str, Any]]:
    """Tín hiệu issue từ sự thật trong cửa sổ. Strength ~ độ trực tiếp của evidence."""
    sig = BaseAgent.signal
    out: list[dict[str, Any]] = []
    order_ref = facts.ref("get_order")
    pay_refs = facts.ref("get_order_payments", "get_payment_timeline")
    paid = _captured_amounts(facts)

    # Đơn hủy / hết hàng nhưng đã thu tiền.
    status = (facts.order_status or "").lower()
    if status in {"canceled", "unavailable"}:
        issue = "canceled_order_paid" if status == "canceled" else "unavailable_order_paid"
        refs = order_ref + pay_refs + (facts.ref("get_order_items") if status == "unavailable"
                                       else [])
        out.append(sig(issue, 0.9 if paid else 0.6, refs))

    # Hoàn tiền: trạng thái mới nhất trong cửa sổ.
    if facts.refunds:
        latest = str(facts.refunds[-1].get("status", "")).lower()
        refund_refs = facts.ref("get_refund_timeline") + pay_refs
        if latest in {"failed", "error", "rejected", "declined"}:
            out.append(sig("refund_failed", 0.9, refund_refs))
        elif latest in {"pending", "processing", "requested", "in_progress"}:
            out.append(sig("refund_pending", 0.9, refund_refs))

    # Đối soát lệch tiền (sự kiện tường minh).
    if facts.mismatches:
        out.append(sig("payment_mismatch", 0.9, facts.ref("get_payment_timeline",
                                                           "get_order_payments")))

    # Nhiều lần thu tiền: trùng (tổng vượt giá trị đơn) hay chia hợp lệ (tổng = giá trị đơn).
    if len(paid) >= 2 and facts.order_total is not None:
        total = sum(paid, Decimal(0))
        repeated = len(set(paid)) < len(paid)
        if repeated and total > facts.order_total + CENT:
            out.append(sig("duplicate_charge", 0.85, pay_refs))
        elif abs(total - facts.order_total) <= CENT and not facts.mismatches:
            out.append(sig("valid_split_payment", 0.85, pay_refs))

    # Giao hàng trễ: ai làm trễ?
    if status not in {"canceled", "unavailable"} and facts.estimated_delivery_at:
        arrived = facts.delivered_customer_at
        late = (arrived is not None and arrived > facts.estimated_delivery_at) or (
            arrived is None and facts.window_end is not None
            and facts.window_end > facts.estimated_delivery_at
        )
        if late:
            seller_late = bool(
                facts.shipping_limit_at and facts.delivered_carrier_at
                and facts.delivered_carrier_at > facts.shipping_limit_at
            )
            issue = "late_delivery_seller" if seller_late else "late_delivery_logistics"
            actor = "seller" if seller_late else "logistics_provider"
            strength = 0.85 if arrived is not None else 0.65
            if actor in facts.late_actors:
                strength += 0.05
            refs = facts.ref("get_shipment_summary") + order_ref
            if seller_late:
                refs += facts.ref("get_order_items", "get_sellers")
            out.append(sig(issue, strength, refs))

    # Đã kiểm tra đủ order + payment + shipment mà không thấy vấn đề → lời khai không đúng.
    core = {"get_order", "get_shipment_summary"} <= set(facts.refs) and bool(pay_refs)
    if not out and core:
        out.append(sig("unsupported_claim", 0.75,
                       order_ref + facts.ref("get_shipment_summary") + pay_refs))
    return out


def stale_conflicts(facts: CaseFacts) -> list[dict[str, Any]]:
    """Bản ghi ngoài dòng thời gian case → data_conflicts (đã chọn nguồn trong cửa sổ)."""
    conflicts = []
    for tool, count in sorted(facts.stale.items()):
        if count <= 0:
            continue
        conflicts.append({
            "field": f"{tool}.records",
            "sources": [f"{tool}:in_case_window", f"{tool}:out_of_case_window"],
            "selected_source": f"{tool}:in_case_window",
            "resolution_code": "OUT_OF_CASE_WINDOW_IGNORED",
        })
    return conflicts[:4]
