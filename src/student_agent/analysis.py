"""Pure business rules over public MCP data (no case-ID or claim-topic shortcuts)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def rows(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number.quantize(CENT) if number.is_finite() and number >= 0 else None
    except (ValueError, InvalidOperation):
        return None


def timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else None
        return result if result and result.tzinfo else None
    except ValueError:
        return None


def total(records: list[dict[str, Any]], key: str) -> Decimal | None:
    amounts = [money(record.get(key)) for record in records]
    if any(amount is None for amount in amounts):
        return None
    return sum((amount for amount in amounts if amount is not None), ZERO)


def event_kind(event: dict[str, Any]) -> str:
    return str(event.get("event_type", "")).lower()


def event_status(event: dict[str, Any]) -> str:
    return str(event.get("status", "")).lower()


def visible_events(
    value: Any, opened_at: datetime, purchased_at: datetime | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Do not use events after the complaint as evidence of its state."""
    visible, future = [], 0
    for event in rows(value):
        occurred = timestamp(event.get("event_at"))
        if occurred and (occurred > opened_at or (purchased_at and occurred < purchased_at)):
            future += 1
        elif event not in visible:
            visible.append(event)
    return visible, future


def unique_ids(records: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(row[key]) for row in records if row.get(key) is not None})


def invoice_total(items: Any, purchased_at: datetime | None, opened_at: datetime) -> Decimal | None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in rows(items):
        if item.get("order_item_id") is None:
            return None
        deadline = timestamp(item.get("shipping_limit_date"))
        if deadline and purchased_at and deadline < purchased_at:
            continue
        groups.setdefault(str(item["order_item_id"]), []).append(item)
    if not groups:
        return None
    amount = ZERO
    for group in groups.values():
        active = [
            item
            for item in group
            if timestamp(item.get("shipping_limit_date"))
            and timestamp(item["shipping_limit_date"]) <= opened_at
        ]
        candidates = active or group
        values = {
            (money(item.get("price")), money(item.get("freight_value"))) for item in candidates
        }
        if len(values) != 1:
            return None
        price, freight = values.pop()
        if price is None or freight is None:
            return None
        amount += price + freight
    return amount


def refund_state(events: list[dict[str, Any]]) -> tuple[str | None, Decimal, bool]:
    refunds = [event for event in events if "refund" in event_kind(event)]
    if not refunds:
        return None, ZERO, True
    # Multiple refund attempts must be reduced independently. When the server
    # exposes no refund identifier, the lifecycle is one ordered stream.
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in refunds:
        reference = str(event.get("refund_id") or event.get("refund_reference") or "order")
        groups.setdefault(reference, []).append(event)
    states: list[str] = []
    refunded = ZERO
    complete = True
    for group in groups.values():
        if len(group) > 1 and any(timestamp(event.get("event_at")) is None for event in group):
            complete = False
            continue
        latest = max(group, key=lambda event: str(event.get("event_at", "")))
        if all(timestamp(event.get("event_at")) is not None for event in group):
            latest = max(group, key=lambda event: timestamp(event["event_at"]))
        text = event_kind(latest) + " " + event_status(latest)
        if any(token in text for token in ("failed", "rejected")):
            states.append("refund_failed")
        elif any(token in text for token in ("pending", "processing", "requested", "initiated")):
            states.append("refund_pending")
        elif any(token in text for token in ("completed", "succeeded", "settled", "refunded")):
            amount = money(latest.get("amount_brl"))
            if amount is None:
                complete = False
            else:
                refunded += amount
        else:
            complete = False
    issue = (
        "refund_failed"
        if "refund_failed" in states
        else ("refund_pending" if "refund_pending" in states else None)
    )
    return issue, refunded, complete


def analyze_payment(
    payments: Any,
    timeline: Any,
    refunds: Any,
    opened_at: datetime,
    purchased_at: datetime | None = None,
    expected_total: Decimal | None = None,
) -> dict[str, Any]:
    base = rows(obj(timeline).get("payments")) if isinstance(timeline, dict) else []
    base = base or rows(payments)
    base = [row for index, row in enumerate(base) if row not in base[:index]]
    events, future = visible_events(obj(timeline).get("events"), opened_at, purchased_at)
    refund_events, _ = visible_events(obj(refunds).get("events"), opened_at, purchased_at)
    all_refunds = [event for event in events if "refund" in event_kind(event)]
    for event in refund_events:
        if event not in all_refunds:
            all_refunds.append(event)
    captures = [
        event
        for event in events
        if event_kind(event) in {"captured", "capture", "capture_succeeded", "duplicate_capture"}
        and event_status(event) in {"confirmed", "succeeded", "completed", "settled", ""}
    ]
    # The gateway can expose rows for several dated lifecycles under one order
    # ID. Untimed base rows are attributable only when they match the in-window
    # captures one-to-one. Never sum unrelated historical/future rows.
    if future and captures and purchased_at:
        needed = Counter(money(event.get("amount_brl")) for event in captures)
        selected = []
        for row in base:
            amount = money(row.get("payment_value"))
            if amount is not None and needed[amount] > 0:
                selected.append(row)
                needed[amount] -= 1
        if not any(needed.values()):
            base = selected
    declared = total(base, "payment_value") if base else None
    captured = total(captures, "amount_brl") if isinstance(timeline, dict) else None
    refund_issue, refunded, refund_complete = refund_state(all_refunds)
    duplicate = any(
        "duplicate" in event_kind(event) and event_status(event) not in {"failed", "rejected"}
        for event in events
    )
    # A repeat of an identical event is not itself a second debit. Different
    # confirmed transaction IDs for the same payment reference provide evidence.
    transaction_groups: dict[str, set[str]] = {}
    for event in captures:
        ref, transaction = event.get("payment_reference"), event.get("transaction_id")
        if ref and transaction:
            transaction_groups.setdefault(str(ref), set()).add(str(transaction))
    duplicate = duplicate or any(len(group) > 1 for group in transaction_groups.values())
    capture_amounts = [money(event.get("amount_brl")) for event in captures]
    inferred_duplicate = bool(
        expected_total is not None
        and captured is not None
        and captured > expected_total
        and len(captures) > 1
        and len(set(capture_amounts)) == 1
        and capture_amounts[0] is not None
        and capture_amounts[0] <= expected_total
    )
    duplicate = duplicate or inferred_duplicate
    references = unique_ids(base, "payment_sequential")
    explicit_refs = unique_ids(base + events, "payment_reference")
    sequences_unique = len(references) == len(base) and len(base) > 0
    comparable = captured is not None and declared is not None and sequences_unique
    mismatch = bool(comparable and captured != declared)
    mismatch = mismatch or any(
        event_kind(event) in {"payment_mismatch", "capture_mismatch", "reconciliation_mismatch"}
        for event in events
    )
    reconciled = bool(comparable and captured == declared and not duplicate and not mismatch)
    split = reconciled and len(references) >= 2
    return {
        "captured": captured,
        "declared": declared,
        "refunded": refunded,
        "remaining": max(ZERO, captured - refunded) if captured is not None else None,
        "duplicate": duplicate,
        "mismatch": mismatch,
        "split": split,
        "reconciled": reconciled,
        "refund_issue": refund_issue,
        "refund_complete": refund_complete,
        "references": explicit_refs or references,
        "future_events": future,
        "ambiguous_base": bool(base and not sequences_unique),
        "has_refund_events": bool(all_refunds),
        "inferred_duplicate": inferred_duplicate,
    }


def analyze_shipment(
    shipment: Any,
    opened_at: datetime,
    order_status: str,
    purchased_at: datetime | None = None,
) -> dict[str, Any]:
    data = obj(shipment)
    events, future = visible_events(data.get("events"), opened_at, purchased_at)
    carrier = timestamp(data.get("delivered_carrier_at"))
    delivered = timestamp(data.get("delivered_customer_at"))
    estimated = timestamp(data.get("estimated_delivery_at"))
    if carrier and carrier > opened_at:
        carrier = None
    if delivered and delivered > opened_at:
        delivered = None
    late = bool(estimated and (delivered or opened_at) > estimated)
    late_sellers: set[str] = set()
    resolved: list[str] = []
    unresolved: list[str] = []
    issue = None
    if order_status not in {"canceled", "unavailable"}:
        explicit = [
            event
            for event in events
            if "late" in event_kind(event) and event_status(event) in {"confirmed", "completed", ""}
        ]
        actors = {str(event.get("actor")) for event in explicit}
        if explicit and delivered and estimated and delivered <= estimated:
            # A complete delivered/estimated pair is more specific than a
            # generic late marker. Preserve the disagreement for audit, but
            # resolve it deterministically instead of discarding the case.
            resolved.append("LATE_EVENT_CONFLICTS_WITH_DELIVERY_TIMESTAMPS")
        elif "seller" in actors and actors.intersection(
            {"logistics", "carrier", "logistics_provider"}
        ):
            unresolved.append("CONFLICTING_DELAY_ACTORS")
        elif "seller" in actors:
            issue = "late_delivery_seller"
            late_sellers.update(
                str(event["seller_id"]) for event in explicit if event.get("seller_id")
            )
        elif actors.intersection({"logistics", "carrier", "logistics_provider"}):
            issue = "late_delivery_logistics"
        limits = rows(data.get("shipping_limits"))
        if purchased_at:
            limits = [
                row
                for row in limits
                if timestamp(row.get("shipping_limit_at"))
                and timestamp(row["shipping_limit_at"]) >= purchased_at
            ]
        if late and carrier and not unresolved:
            for limit in limits:
                deadline = timestamp(limit.get("shipping_limit_at"))
                if deadline and carrier > deadline and limit.get("seller_id"):
                    late_sellers.add(str(limit["seller_id"]))
            if late_sellers:
                if issue == "late_delivery_logistics":
                    unresolved.append("HANDOFF_CONFLICTS_WITH_DELAY_ACTOR")
                else:
                    issue = "late_delivery_seller"
            elif limits and all(timestamp(row.get("shipping_limit_at")) for row in limits):
                issue = issue or "late_delivery_logistics"
        if issue == "late_delivery_seller" and not late_sellers:
            candidates = unique_ids(limits, "seller_id")
            if len(candidates) == 1:
                late_sellers.update(candidates)
    return {
        "issue": None if unresolved else issue,
        "late_sellers": sorted(late_sellers),
        "future_events": future,
        "complete": bool(delivered and estimated and not unresolved),
        "resolved": resolved,
        "unresolved": unresolved,
        "shipment_ids": unique_ids(events, "shipment_id"),
    }


def choose_issue(order: dict[str, Any], payment: dict[str, Any], shipment: dict[str, Any]) -> str:
    status, paid = order.get("order_status"), payment["captured"]
    if payment["refund_issue"]:
        return payment["refund_issue"]
    if payment["duplicate"]:
        return "duplicate_charge"
    if payment["mismatch"]:
        return "payment_mismatch"
    if status in {"canceled", "unavailable"}:
        if paid is None:
            return "insufficient_evidence"
        if paid > 0 and payment["remaining"] > 0:
            return f"{status}_order_paid"
    if shipment["issue"]:
        return shipment["issue"]
    if payment["split"] and not shipment["unresolved"]:
        return "valid_split_payment"
    if payment["reconciled"] and shipment["complete"] and payment["refund_complete"]:
        return "unsupported_claim"
    return "insufficient_evidence"
