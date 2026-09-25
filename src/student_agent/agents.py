"""Domain specialists: order/item, payment/refund and shipment agents.

Each agent only calls the MCP tools of its own domain (``ledger.TOOL_MAPPING``), turns
validated evidence into findings that carry the supporting ``evidence_ref`` and emits
``tool_result_consumed`` for evidence it actually used. A failed tool call is a
coverage gap, never negative evidence.

Evidence rows for one order can include rows from another time window (a different
purchase cluster). Specialists anchor on the order's purchase timestamp and keep only
rows inside that order's own timeline; excluded rows are reported as warnings.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .a2a import (
    ORDER_AGENT,
    PAYMENT_AGENT,
    SHIPMENT_AGENT,
    AgentContext,
    AgentResult,
    AgentTask,
    DataConflict,
    DomainResult,
    DomainTaskPayload,
    EntityIds,
    Finding,
    HandoffRequest,
)
from .ledger import EvidenceRecord, thaw
from .mcp_gateway import GatewayError, GatewayFatalError

# Relevance windows relative to the order purchase timestamp (rules l3a-rules-v2).
CAPTURE_WINDOW = (timedelta(hours=-1), timedelta(days=1))
ITEM_LIMIT_WINDOW = (timedelta(0), timedelta(days=6))
REFUND_WINDOW = (timedelta(0), timedelta(days=25))
PAYMENT_EVENT_WINDOW = (timedelta(hours=-1), timedelta(days=2))
DELIVERY_EVENT_TOLERANCE = timedelta(days=1)


# ----------------------------------------------------------------- data helpers
def rows(data: Any) -> list[dict[str, Any]]:
    """Return the row objects of an evidence ``data`` payload (list or single object)."""
    value = thaw(data)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def nested_rows(data: Any, key: str) -> list[dict[str, Any]]:
    value = thaw(data)
    if isinstance(value, dict) and isinstance(value.get(key), list):
        return [row for row in value[key] if isinstance(row, dict)]
    return []


def distinct(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop rows that are exact copies (same fields and timestamps) of an earlier row."""
    seen, result = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def get(row: Mapping[str, Any] | None, *names: str) -> Any:
    if not row:
        return None
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def within(
    moment: datetime | None, anchor: datetime | None, window: tuple[timedelta, timedelta]
) -> bool:
    if moment is None or anchor is None:
        return anchor is None  # without an anchor nothing can be excluded
    try:
        return anchor + window[0] <= moment <= anchor + window[1]
    except TypeError:  # naive vs aware
        return True


def lower(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def purchase_anchor(findings: Iterable[Finding]) -> datetime | None:
    for finding in findings:
        if finding.finding_code == "ORDER_STATUS" and isinstance(finding.value, dict):
            return when(finding.value.get("purchased_at"))
    return None


class _Specialist:
    actor: str = ""

    def __init__(self) -> None:
        self._counter = 0

    async def _fetch(
        self, context: AgentContext, gaps: list[str], tool: str, **arguments: str
    ) -> EvidenceRecord | None:
        evidence = context.gateway
        if evidence is None:
            gaps.append(f"{tool}:NO_GATEWAY")
            return None
        try:
            return await evidence.fetch(self.actor, tool, deadline=context.deadline, **arguments)
        except GatewayFatalError:
            raise
        except GatewayError as exc:
            gaps.append(f"{tool}:{exc.code}")
            return None

    def _finding(
        self,
        task: AgentTask,
        code: str,
        value: Any,
        records: Iterable[EvidenceRecord],
        entity_ids: EntityIds | None = None,
    ) -> Finding:
        self._counter += 1
        payload = task.payload
        claim_ids = tuple(c.claim_id for c in getattr(payload, "claims", ()))
        refs = tuple(dict.fromkeys(r.evidence_ref for r in records))
        return Finding(
            finding_id=f"{task.task_id}-{self.actor.split('-')[0].upper()}-{self._counter:02d}",
            finding_code=code,
            value=value,
            entity_ids=entity_ids or EntityIds(),
            claim_ids=claim_ids,
            evidence_refs=refs,
        )

    def _result(
        self,
        task: AgentTask,
        context: AgentContext,
        findings: list[Finding],
        entities: EntityIds,
        gaps: list[str],
        used: list[EvidenceRecord],
        conflicts: tuple[DataConflict, ...] = (),
        handoffs: tuple[HandoffRequest, ...] = (),
    ) -> AgentResult:
        if used and context.gateway is not None and not context.repair_reason_codes:
            context.gateway.consume(self.actor, used)
        refs = tuple(dict.fromkeys(ref for f in findings for ref in f.evidence_refs))
        if handoffs:
            status = "needs_handoff"
        elif findings:
            status = "completed"
        else:
            status = "insufficient_evidence"
        payload = DomainResult(
            findings=tuple(findings),
            affected_entities=entities,
            evidence_refs=refs,
            conflicts=conflicts,
            warnings=tuple(sorted(set(gaps))),
            handoff_requests=handoffs,
        )
        return AgentResult(
            task.local_run_id, task.case_id, task.task_id, self.actor, status, payload
        )

    @staticmethod
    def _payload(task: AgentTask) -> DomainTaskPayload:
        payload = task.payload
        assert isinstance(payload, DomainTaskPayload)
        return payload


# ---------------------------------------------------------------- order agent
class OrderAgent(_Specialist):
    actor = ORDER_AGENT

    async def handle(self, task: AgentTask, context: AgentContext) -> AgentResult:
        self._counter = 0
        findings: list[Finding] = []
        gaps: list[str] = []
        used: list[EvidenceRecord] = []
        entities = EntityIds()
        for order_id in self._payload(task).entity_ids.order_ids:
            order = await self._fetch(context, gaps, "get_order", order_id=order_id)
            anchor = None
            if order is not None and rows(order.data):
                row = rows(order.data)[0]
                if get(row, "order_id") not in (None, order_id):
                    gaps.append("get_order:ORDER_ID_MISMATCH")
                else:
                    anchor = when(get(row, "order_purchase_timestamp"))
                    used.append(order)
                    order_entities = EntityIds(order_ids=(order_id,))
                    entities = entities.union(order_entities)
                    findings.append(
                        self._finding(
                            task,
                            "ORDER_STATUS",
                            {
                                "order_id": order_id,
                                "status": lower(get(row, "order_status")) or None,
                                "purchased_at": get(row, "order_purchase_timestamp"),
                                "approved_at": get(row, "order_approved_at"),
                                "delivered_carrier_at": get(row, "order_delivered_carrier_date"),
                                "delivered_customer_at": get(row, "order_delivered_customer_date"),
                                "estimated_delivery_at": get(row, "order_estimated_delivery_date"),
                            },
                            [order],
                            order_entities,
                        )
                    )
            items = await self._fetch(context, gaps, "get_order_items", order_id=order_id)
            if items is None:
                continue
            kept, dropped = [], 0
            for row in distinct(rows(items.data)):
                limit = when(get(row, "shipping_limit_date"))
                if within(limit, anchor, ITEM_LIMIT_WINDOW):
                    kept.append(row)
                else:
                    dropped += 1
            if dropped:
                gaps.append(f"get_order_items:{dropped}_ROWS_OUTSIDE_ORDER_TIMELINE")
            if not kept:
                continue
            lines = []
            for row in kept:
                price = money(get(row, "price")) or Decimal("0")
                freight = money(get(row, "freight_value")) or Decimal("0")
                lines.append(
                    {
                        "item_id": get(row, "order_item_id"),
                        "product_id": get(row, "product_id"),
                        "seller_id": get(row, "seller_id"),
                        "price": str(price),
                        "freight": str(freight),
                        "shipping_limit_at": get(row, "shipping_limit_date"),
                    }
                )
            item_entities = EntityIds(
                order_ids=(order_id,),
                item_ids=tuple(str(line["item_id"]) for line in lines if line["item_id"]),
                seller_ids=tuple(str(line["seller_id"]) for line in lines if line["seller_id"]),
            )
            entities = entities.union(item_entities)
            used.append(items)
            price_total = sum((Decimal(line["price"]) for line in lines), Decimal("0"))
            freight_total = sum((Decimal(line["freight"]) for line in lines), Decimal("0"))
            findings.append(
                self._finding(
                    task,
                    "ORDER_ITEMS",
                    {
                        "lines": lines,
                        "items_total": str(price_total),
                        "freight_total": str(freight_total),
                        "order_total": str(price_total + freight_total),
                    },
                    [items],
                    item_entities,
                )
            )
        return self._result(task, context, findings, entities, gaps, used)


# -------------------------------------------------------------- payment agent
class PaymentAgent(_Specialist):
    actor = PAYMENT_AGENT

    async def handle(self, task: AgentTask, context: AgentContext) -> AgentResult:
        self._counter = 0
        findings: list[Finding] = []
        gaps: list[str] = []
        used: list[EvidenceRecord] = []
        entities = EntityIds()
        payload = self._payload(task)
        anchor = purchase_anchor(payload.findings)
        if anchor is None:
            gaps.append("NO_PURCHASE_ANCHOR")
        for order_id in payload.entity_ids.order_ids:
            timeline = await self._fetch(context, gaps, "get_payment_timeline", order_id=order_id)
            if timeline is not None:
                captures, other, dropped = [], [], 0
                for event in distinct(nested_rows(timeline.data, "events")):
                    kind = lower(get(event, "event_type"))
                    at = when(get(event, "event_at"))
                    window = CAPTURE_WINDOW if kind == "captured" else PAYMENT_EVENT_WINDOW
                    if not within(at, anchor, window):
                        dropped += 1
                        continue
                    compact = {
                        "event": kind,
                        "status": lower(get(event, "status")),
                        "amount": str(money(get(event, "amount_brl", "amount")) or "0.00"),
                        "at": get(event, "event_at"),
                    }
                    (captures if kind == "captured" else other).append(compact)
                if dropped:
                    gaps.append(f"get_payment_timeline:{dropped}_EVENTS_OUTSIDE_ORDER_TIMELINE")
                captured = [c for c in captures if c["status"] in ("confirmed", "")]
                amounts = [Decimal(c["amount"]) for c in captured]
                base = nested_rows(timeline.data, "payments")
                kept_rows = _match_payment_rows(base, amounts)
                if captured or other:
                    used.append(timeline)
                    findings.append(
                        self._finding(
                            task,
                            "PAYMENTS",
                            {
                                "captures": captured,
                                "events": other,
                                "total_captured": str(sum(amounts, Decimal("0"))),
                                "rows": kept_rows,
                                "anchored": anchor is not None,
                            },
                            [timeline],
                            EntityIds(order_ids=(order_id,)),
                        )
                    )
                    entities = entities.union(EntityIds(order_ids=(order_id,)))
            refunds = await self._fetch(context, gaps, "get_refund_timeline", order_id=order_id)
            if refunds is not None:
                kept, dropped = [], 0
                for event in distinct(nested_rows(refunds.data, "events") or rows(refunds.data)):
                    at = when(get(event, "event_at"))
                    if get(event, "event_type", "status") is None:
                        continue
                    if not within(at, anchor, REFUND_WINDOW):
                        dropped += 1
                        continue
                    kept.append(
                        {
                            "event": lower(get(event, "event_type")),
                            "status": lower(get(event, "status")),
                            "amount": str(money(get(event, "amount_brl", "amount")) or "0.00"),
                            "at": get(event, "event_at"),
                        }
                    )
                if dropped:
                    gaps.append(f"get_refund_timeline:{dropped}_EVENTS_OUTSIDE_ORDER_TIMELINE")
                if kept:
                    used.append(refunds)
                    findings.append(
                        self._finding(
                            task,
                            "REFUNDS",
                            kept,
                            [refunds],
                            EntityIds(order_ids=(order_id,)),
                        )
                    )
        return self._result(task, context, findings, entities, gaps, used)


def _match_payment_rows(base: list[dict[str, Any]], amounts: list[Decimal]) -> list[dict[str, Any]]:
    """Keep base payment rows whose value matches a relevant capture (multiset match)."""
    remaining = list(amounts)
    kept = []
    for row in base:
        value = money(get(row, "payment_value"))
        if value in remaining:
            remaining.remove(value)
            kept.append(
                {
                    "sequential": get(row, "payment_sequential"),
                    "type": get(row, "payment_type"),
                    "installments": get(row, "payment_installments"),
                    "value": str(value),
                }
            )
    return kept


# ------------------------------------------------------------- shipment agent
class ShipmentAgent(_Specialist):
    actor = SHIPMENT_AGENT

    async def handle(self, task: AgentTask, context: AgentContext) -> AgentResult:
        self._counter = 0
        findings: list[Finding] = []
        gaps: list[str] = []
        used: list[EvidenceRecord] = []
        entities = EntityIds()
        payload = self._payload(task)
        anchor = purchase_anchor(payload.findings)
        for order_id in payload.entity_ids.order_ids:
            record = await self._fetch(context, gaps, "get_shipment_summary", order_id=order_id)
            if record is None or not rows(record.data):
                continue
            base = rows(record.data)[0]
            delivered = when(get(base, "delivered_customer_at"))
            estimated = when(get(base, "estimated_delivery_at"))
            carrier = when(get(base, "delivered_carrier_at"))
            limits = []
            for row in nested_rows(record.data, "shipping_limits"):
                limit = when(get(row, "shipping_limit_at"))
                if within(limit, anchor, ITEM_LIMIT_WINDOW):
                    limits.append(limit)
            limit = max(limits) if limits else None
            late_events, dropped = [], 0
            for event in nested_rows(record.data, "events"):
                at = when(get(event, "event_at"))
                relevant = (
                    delivered is not None
                    and at is not None
                    and abs(at - delivered) <= DELIVERY_EVENT_TOLERANCE
                )
                if not relevant:
                    dropped += 1
                    continue
                late_events.append(
                    {
                        "event": lower(get(event, "event_type")),
                        "actor": lower(get(event, "actor")),
                        "status": lower(get(event, "status")),
                        "at": get(event, "event_at"),
                    }
                )
            if dropped:
                gaps.append(f"get_shipment_summary:{dropped}_EVENTS_OUTSIDE_DELIVERY")
            late = bool(delivered and estimated and delivered > estimated)
            summary = {
                "status": lower(get(base, "order_status")) or None,
                "delivered_customer_at": get(base, "delivered_customer_at"),
                "estimated_delivery_at": get(base, "estimated_delivery_at"),
                "delivered_carrier_at": get(base, "delivered_carrier_at"),
                "delivered_late": late,
                "late_days": (delivered - estimated).days if late else 0,
                "seller_handoff_late": bool(carrier and limit and carrier > limit),
                "events": late_events,
            }
            used.append(record)
            ship_entities = EntityIds(order_ids=(order_id,))
            entities = entities.union(ship_entities)
            findings.append(self._finding(task, "SHIPMENT", summary, [record], ship_entities))
        return self._result(task, context, findings, entities, gaps, used)
