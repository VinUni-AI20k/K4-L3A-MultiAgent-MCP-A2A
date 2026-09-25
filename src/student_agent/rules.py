"""Deterministic L3A decision rules over MCP evidence.

Every order in the gateway carries decoy rows (items, captures, refunds, shipment events)
dated outside the case window [order purchase, case opened_at], or exact replicas of a real
row. Rules only use distinct records inside that window; excluded records are reported as
data conflicts, never as facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

CENT = Decimal("0.01")

ISSUE_PRIORITY = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "refund_failed",
    "refund_pending",
    "payment_mismatch",
    "duplicate_charge",
    "valid_split_payment",
    "late_delivery_seller",
    "late_delivery_logistics",
)
NO_ACTION_ISSUES = {"valid_split_payment", "unsupported_claim"}


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(CENT)
    except (ArithmeticError, ValueError):
        return Decimal("0.00")


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def contains(self, value: Any) -> bool:
        moment = parse_time(value)
        return moment is not None and self.start <= moment <= self.end


@dataclass
class Facts:
    """Normalized, in-window view of one order assembled from specialist reports."""

    order_id: str
    order_status: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    captures: list[dict[str, Any]] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    shipment_events: list[dict[str, Any]] = field(default_factory=list)
    payment_references: list[str] = field(default_factory=list)
    shipment_ids: list[str] = field(default_factory=list)
    delivered_carrier_at: datetime | None = None
    delivered_customer_at: datetime | None = None
    estimated_delivery_at: datetime | None = None
    excluded: dict[str, int] = field(default_factory=dict)

    @property
    def seller_ids(self) -> list[str]:
        return sorted({item["seller_id"] for item in self.items if item.get("seller_id")})

    @property
    def item_ids(self) -> list[str]:
        return sorted({item["order_item_id"] for item in self.items if item.get("order_item_id")})

    @property
    def order_total(self) -> Decimal:
        return sum(
            (money(item.get("price")) + money(item.get("freight_value")) for item in self.items),
            Decimal("0.00"),
        )

    @property
    def price_total(self) -> Decimal:
        return sum((money(item.get("price")) for item in self.items), Decimal("0.00"))

    @property
    def freight_total(self) -> Decimal:
        return sum((money(item.get("freight_value")) for item in self.items), Decimal("0.00"))

    @property
    def captured_total(self) -> Decimal:
        return sum((money(event.get("amount_brl")) for event in self.captures), Decimal("0.00"))

    @property
    def shipping_limit(self) -> datetime | None:
        limits = [parse_time(item.get("shipping_limit_date")) for item in self.items]
        present = [limit for limit in limits if limit is not None]
        return max(present) if present else None


def one_row_per_item(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep one row per order_item_id: the earliest shipping limit, closest to the purchase."""
    chosen: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("order_item_id"))
        limit = parse_time(item.get("shipping_limit_date"))
        current = chosen.get(key)
        if current is None or (limit and limit < parse_time(current.get("shipping_limit_date"))):
            chosen[key] = item
    return list(chosen.values()), len(items) - len(chosen)


def split_in_window(
    records: list[dict[str, Any]], time_field: str, window: Window
) -> tuple[list[dict[str, Any]], int]:
    """Keep in-window records, dropping exact replicas (same fields and same timestamp)."""
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = json.dumps(record, sort_keys=True, default=str)
        if key in seen or not window.contains(record.get(time_field)):
            continue
        seen.add(key)
        kept.append(record)
    return kept, len(records) - len(kept)


def _duplicate_amount(facts: Facts) -> Decimal | None:
    amounts = [money(event.get("amount_brl")) for event in facts.captures]
    repeated = sorted({amount for amount in amounts if amounts.count(amount) > 1}, reverse=True)
    if repeated and facts.captured_total > facts.order_total:
        return repeated[0]
    return None


def detect_issues(facts: Facts) -> dict[str, str]:
    """Return every issue whose data condition holds, mapped to an observable reason code."""
    found: dict[str, str] = {}
    paid = facts.captured_total > 0
    completed_refund = any(event.get("status") == "completed" for event in facts.refunds)
    if facts.order_status == "canceled" and paid and not completed_refund:
        found["canceled_order_paid"] = "CANCELED_WITH_CAPTURE"
    if facts.order_status == "unavailable" and paid and not completed_refund:
        found["unavailable_order_paid"] = "UNAVAILABLE_WITH_CAPTURE"
    statuses = {event.get("status") for event in facts.refunds}
    if "failed" in statuses:
        found["refund_failed"] = "REFUND_EVENT_FAILED"
    if "pending" in statuses:
        found["refund_pending"] = "REFUND_EVENT_PENDING"
    if any(event.get("status") == "open" for event in facts.mismatches):
        found["payment_mismatch"] = "OPEN_RECONCILIATION_MISMATCH"
    if _duplicate_amount(facts) is not None:
        found["duplicate_charge"] = "REPEATED_CAPTURE_ABOVE_ORDER_TOTAL"
    elif len(facts.captures) > 1 and facts.captured_total == facts.order_total:
        found["valid_split_payment"] = "SPLIT_CAPTURES_MATCH_ORDER_TOTAL"
    late = (
        facts.delivered_customer_at is not None
        and facts.estimated_delivery_at is not None
        and facts.delivered_customer_at > facts.estimated_delivery_at
    )
    if late:
        limit = facts.shipping_limit
        if facts.delivered_carrier_at and limit and facts.delivered_carrier_at > limit:
            found["late_delivery_seller"] = "CARRIER_HANDOFF_AFTER_SHIPPING_LIMIT"
        else:
            found["late_delivery_logistics"] = "HANDOFF_ON_TIME_DELIVERY_LATE"
    return found


def choose_primary(found: dict[str, str], claimed_topic: str | None) -> tuple[str, float]:
    """Prefer the customer's claim only when the data independently confirms it."""
    if claimed_topic in found:
        confidence = 0.9 if len(found) == 1 else 0.8
        return claimed_topic, confidence
    for issue in ISSUE_PRIORITY:
        if issue in found:
            return issue, 0.6
    return "unsupported_claim", 0.8 if claimed_topic == "unsupported_claim" else 0.7


def refund_amount(issue: str, facts: Facts) -> Decimal:
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return min(facts.price_total, facts.captured_total)
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        return min(facts.freight_total, facts.captured_total)
    if issue == "duplicate_charge":
        return _duplicate_amount(facts) or Decimal("0.00")
    if issue == "payment_mismatch":
        open_events = [event for event in facts.mismatches if event.get("status") == "open"]
        return sum((money(event.get("amount_brl")) for event in open_events), Decimal("0.00"))
    if issue == "refund_failed":
        failed = [event for event in facts.refunds if event.get("status") == "failed"]
        return sum((money(event.get("amount_brl")) for event in failed), Decimal("0.00"))
    return Decimal("0.00")


def responsible_parties(
    template: list[dict[str, Any]], facts: Facts
) -> list[dict[str, str | None]]:
    """Policy party types with ids resolved from this order; policy ids are only examples."""
    parties: list[dict[str, str | None]] = []
    for party in template:
        party_type = party.get("party_type", "unknown")
        if party_type == "seller":
            parties.extend(
                {"party_type": "seller", "party_id": seller} for seller in facts.seller_ids
            )
        else:
            parties.append({"party_type": party_type, "party_id": None})
    return parties[:5] or [{"party_type": "unknown", "party_id": None}]


def full_refund_verdict(refund: Decimal, facts: Facts) -> str:
    if refund <= 0:
        return "unsupported"
    if refund >= facts.captured_total:
        return "supported"
    return "partially_supported"
