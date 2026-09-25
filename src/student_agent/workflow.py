from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PERMISSIONS = {
    "order-agent": {"get_order": "order", "get_order_items": "item"},
    "payment-agent": {"get_payment_timeline": "payment", "get_refund_timeline": "refund"},
    "shipment-agent": {"get_shipment_summary": "shipment"},
    "policy-agent": {"get_policy": "policy"},
}
ZERO = Decimal("0.00")


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Invalid monetary amount")
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Invalid monetary amount") from exc


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Missing evidence timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Evidence timestamp must include a timezone")
    return result


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Expected evidence rows")
    return value


def _check_scope(value: Any, order_id: str) -> None:
    if isinstance(value, dict):
        if "order_id" in value and value["order_id"] != order_id:
            raise ValueError("Evidence contains an order outside this case")
        for child in value.values():
            _check_scope(child, order_id)
    elif isinstance(value, list):
        for child in value:
            _check_scope(child, order_id)


class _CaseWorkflow:
    """Case-local A2A state; no evidence or decisions are shared between cases."""

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter):
        self.case = case
        self.gateway = gateway
        self.trace = trace
        self.case_id = case["case_id"]
        self.order_id = case["customer_request"]["claimed_order_id"]
        self.evidence: dict[str, dict[str, Any]] = {}
        self.failures: set[str] = set()
        self.conflicts: list[dict[str, Any]] = []
        self.tools: set[str] = set()

    def emit(self, event_type: str, actor: str, **fields: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **fields)

    def conflict(self, field: str, sources: list[str], selected: str | None, code: str) -> None:
        entry = dict(field=field, sources=sources, selected_source=selected, resolution_code=code)
        if entry not in self.conflicts:
            self.conflicts.append(entry)

    async def collect(self, actor: str, tool: str) -> Any:
        domain = PERMISSIONS[actor][tool]
        self.emit("task_assigned", "coordinator", target=actor, tool_name=tool)
        arguments = (
            {"policy_version": self.case["policy_version"]}
            if tool == "get_policy"
            else {"order_id": self.order_id}
        )
        code = "TOOL_UNAVAILABLE"
        if tool in self.tools:
            for attempt in range(3):
                try:
                    async with asyncio.timeout(45):
                        envelope = await self.gateway.call(tool, case_id=self.case_id, **arguments)
                    self.trace.contracts.validate_evidence(envelope)
                    if envelope["domain"] != domain:
                        raise ValueError("Unexpected evidence domain")
                    data = envelope["data"]
                    _check_scope(data, self.order_id)
                    if tool == "get_order_items":
                        _rows(data)
                    elif not isinstance(data, dict):
                        raise ValueError("Expected evidence object")
                    elif tool != "get_policy" and data.get("order_id") != self.order_id:
                        raise ValueError("Missing or mismatched evidence order")
                    if (
                        tool == "get_policy"
                        and data.get("policy_version") != arguments["policy_version"]
                    ):
                        raise ValueError("Mismatched policy version")
                    self.evidence[tool] = envelope
                    self.emit(
                        "tool_result_consumed",
                        actor,
                        tool_name=tool,
                        evidence_refs=[envelope["evidence_ref"]],
                    )
                    self.emit(
                        "handoff",
                        actor,
                        target="policy-agent",
                        tool_name=tool,
                        decision_code="EVIDENCE_READY",
                        evidence_refs=[envelope["evidence_ref"]],
                    )
                    return data
                except (TimeoutError, httpx2.TransportError):
                    code = "MCP_TRANSIENT_FAILURE"
                    if attempt < 2:
                        self.emit(
                            "task_assigned",
                            "coordinator",
                            target=actor,
                            tool_name=tool,
                            decision_code="RETRY",
                            attributes={"attempt": attempt + 2},
                        )
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                except httpx2.HTTPStatusError as exc:
                    code = "MCP_HTTP_FAILURE"
                    if exc.response.status_code in {429, 502, 503, 504} and attempt < 2:
                        self.emit(
                            "task_assigned",
                            "coordinator",
                            target=actor,
                            tool_name=tool,
                            decision_code="RETRY",
                            attributes={"attempt": attempt + 2},
                        )
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                except (RuntimeError, ValueError):
                    # Do not retry access errors, invalid contracts or ambiguous server errors.
                    code = "INVALID_OR_UNAVAILABLE_EVIDENCE"
                break
        self.failures.add(tool)
        self.emit("handoff", actor, target="policy-agent", tool_name=tool, decision_code=code)
        return None

    def events(
        self, data: dict[str, Any], purchase: datetime, opened: datetime, tool: str
    ) -> list[dict[str, Any]]:
        rows = _rows(data["events"])
        selected = [row for row in rows if purchase <= _time(row["event_at"]) <= opened]
        if len(selected) != len(rows):
            self.conflict(
                "events.event_at",
                ["get_order", tool],
                tool,
                "FILTER_TO_PURCHASE_AND_CASE_OPENED_AT",
            )
        return sorted(selected, key=lambda row: _time(row["event_at"]))

    def decide(
        self, order: Any, items: Any, payment: Any, shipment: Any, refund: Any, policy: Any
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        issue = "insufficient_evidence"
        used = {"get_order", "get_order_items", "get_payment_timeline"}
        selected_items: list[dict[str, Any]] = []
        captures: list[dict[str, Any]] = []
        paid = ZERO
        late_sellers: set[str] = set()
        try:
            if not all(isinstance(data, dict) for data in (order, payment, policy)):
                raise ValueError("Missing core evidence")
            purchase = _time(order["order_purchase_timestamp"])
            opened = _time(self.case["opened_at"])
            estimated = _time(order["order_estimated_delivery_date"])
            if opened < purchase or estimated < purchase:
                raise ValueError("Inconsistent order dates")
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in _rows(items):
                grouped.setdefault(str(row["order_item_id"]), []).append(row)
            for rows in grouped.values():
                eligible = [
                    row
                    for row in rows
                    if purchase <= _time(row["shipping_limit_date"]) <= estimated
                ]
                unique = [row for i, row in enumerate(eligible) if row not in eligible[:i]]
                if len(rows) > 1:
                    self.conflict(
                        "order_items",
                        ["get_order", "get_order_items"],
                        "get_order_items" if len(unique) == 1 else None,
                        "DEDUPLICATE_WITHIN_ORDER_TIMELINE",
                    )
                if len(unique) != 1:
                    raise ValueError("Ambiguous item snapshot")
                if not isinstance(unique[0].get("seller_id"), str) or not unique[0]["seller_id"]:
                    raise ValueError("Missing seller identity")
                _money(unique[0]["price"])
                _money(unique[0]["freight_value"])
                selected_items.extend(unique)
            if not selected_items:
                raise ValueError("Missing order items")
            events = self.events(payment, purchase, opened, "get_payment_timeline")
            captures = [
                e
                for e in events
                if e["event_type"] == "captured"
                and e.get("status") in {"confirmed", "completed", "succeeded"}
            ]
            paid = sum((_money(e["amount_brl"]) for e in captures), ZERO)
            total = sum(
                (_money(r["price"]) + _money(r["freight_value"]) for r in selected_items), ZERO
            )
            refund_events = []
            if refund is not None:
                used.add("get_refund_timeline")
                refund_events = self.events(refund, purchase, opened, "get_refund_timeline")
            refund_requested = any(
                c.get("topic", "").startswith("refund_")
                for c in self.case["customer_request"].get("claims", [])
            )
            if refund_requested and refund is None:
                raise ValueError("Refund claim requires refund lifecycle evidence")
            latest_refund = refund_events[-1] if refund_events else {}
            refund_status = latest_refund.get("status")
            if refund_status == "failed":
                issue = "refund_failed"
            elif refund_status in {"pending", "processing", "requested"}:
                issue = "refund_pending"
            elif order["order_status"] in {"canceled", "unavailable"} and paid > ZERO:
                if refund_status in {"completed", "succeeded", "confirmed"}:
                    raise ValueError("Completed refund needs separate reconciliation")
                issue = f"{order['order_status']}_order_paid"
            elif any(
                e["event_type"] == "reconciliation_mismatch" and e.get("status") == "open"
                for e in events
            ):
                issue = "payment_mismatch"
            else:
                used.add("get_shipment_summary")
                if not isinstance(shipment, dict):
                    raise ValueError("Missing shipment evidence")
                for field, key in (
                    ("delivered_carrier_at", "order_delivered_carrier_date"),
                    ("delivered_customer_at", "order_delivered_customer_date"),
                    ("estimated_delivery_at", "order_estimated_delivery_date"),
                ):
                    if shipment.get(field) != order.get(key):
                        self.conflict(
                            field,
                            ["get_order", "get_shipment_summary"],
                            None,
                            "UNRESOLVED_SOURCE_CONFLICT",
                        )
                        raise ValueError("Conflicting shipment timestamps")
                delivered = shipment.get("delivered_customer_at")
                delivered_at = _time(delivered) if delivered else None
                late = min(delivered_at, opened) > estimated if delivered_at else opened > estimated
                if late:
                    carrier = _time(shipment["delivered_carrier_at"])
                    late_sellers = {
                        row["seller_id"]
                        for row in selected_items
                        if carrier > _time(row["shipping_limit_date"])
                    }
                    issue = "late_delivery_seller" if late_sellers else "late_delivery_logistics"
                elif (
                    len(captures) > 1
                    and paid > total
                    and len({_money(e["amount_brl"]) for e in captures}) == 1
                ):
                    issue = "duplicate_charge"
                elif paid != total:
                    issue = "payment_mismatch"
                elif len(captures) > 1:
                    issue = "valid_split_payment"
                elif delivered_at and delivered_at <= opened and paid > ZERO:
                    issue = "unsupported_claim"
                else:
                    raise ValueError("No conclusive evidence")
        except (KeyError, TypeError, ValueError):
            issue = "insufficient_evidence"

        rule = None
        amount = ZERO
        parties: list[dict[str, Any]] = []
        status = "needs_investigation"
        actions = ["investigate_missing_or_conflicting_evidence"]
        if issue != "insufficient_evidence":
            try:
                rule = policy["rules"][issue]
                if policy["currency"] != "BRL":
                    raise ValueError("Unexpected policy currency")
                amount = _money(rule["refund_brl"])
                status = rule["case_status"]
                actions = [rule["recommended_action"]]
                parties = [dict(party) for party in _rows(rule["responsible_parties"])]
                # Policy templates can contain seller IDs belonging to a different order.
                sellers = late_sellers or {row["seller_id"] for row in selected_items}
                if any(p["party_type"] == "seller" for p in parties):
                    declared = {p["party_id"] for p in parties if p["party_type"] == "seller"}
                    if declared != sellers:
                        self.conflict(
                            "responsible_parties.party_id",
                            ["get_policy", "get_order_items"],
                            "get_order_items",
                            "USE_SCOPED_SELLER_IDS",
                        )
                    parties = [p for p in parties if p["party_type"] != "seller"] + [
                        {"party_type": "seller", "party_id": seller} for seller in sorted(sellers)
                    ]
                if amount > paid or (status == "no_action" and amount != ZERO):
                    raise ValueError("Policy refund conflicts with captured amount or status")
                used.add("get_policy")
            except (KeyError, TypeError, ValueError):
                issue, status, amount, rule = (
                    "insufficient_evidence",
                    "needs_investigation",
                    ZERO,
                    None,
                )
                parties, actions = [], ["investigate_missing_or_conflicting_evidence"]

        # Conflicts also need their source references in the final evidence set.
        for conflict in self.conflicts:
            used.update(conflict["sources"])
        refs = [
            self.evidence[name]["evidence_ref"] for name in sorted(used) if name in self.evidence
        ]
        warning_count = sum(
            bool(self.evidence[name].get("warnings")) for name in used if name in self.evidence
        )
        confidence = max(0.5, 0.96 - 0.07 * len(self.conflicts) - 0.05 * warning_count)
        if issue == "duplicate_charge":
            confidence = min(confidence, 0.75)  # Equal captures alone lack transaction identity.
        if issue == "insufficient_evidence":
            confidence = 0.25
        entities = {
            "order_ids": [self.order_id] if "get_order" in self.evidence else [],
            "item_ids": sorted({str(row["order_item_id"]) for row in selected_items}),
            "seller_ids": sorted({row["seller_id"] for row in selected_items}),
            "payment_references": sorted(
                {str(row["payment_reference"]) for row in captures if row.get("payment_reference")}
            ),
            "shipment_ids": (
                [shipment["shipment_id"]]
                if isinstance(shipment, dict)
                and shipment.get("shipment_id")
                and "get_shipment_summary" in used
                else []
            ),
        }
        claims = []
        for claim in self.case["customer_request"].get("claims", []):
            verdict = "insufficient_evidence"
            if issue != "insufficient_evidence":
                if claim["topic"] == "requested_full_refund":
                    verdict = (
                        "supported"
                        if paid > ZERO and amount == paid
                        else "partially_supported"
                        if amount > ZERO
                        else "unsupported"
                    )
                elif claim["topic"] in policy["rules"]:
                    verdict = "supported" if claim["topic"] == issue else "unsupported"
            claims.append(
                dict(
                    claim_id=claim["claim_id"],
                    verdict=verdict,
                    confidence=round(confidence, 2),
                    evidence_refs=refs,
                )
            )
        output = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": self.case_id,
            "assessment": dict(
                primary_issue=issue, case_status=status, confidence=round(confidence, 2)
            ),
            "affected_entities": entities,
            "claim_assessments": claims,
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
                "responsible_parties": parties,
            },
            "evidence_refs": refs,
            "data_conflicts": self.conflicts[:5],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": float(amount),
                "refund_lines": (
                    [
                        dict(
                            reason_code=issue.upper(),
                            amount_brl=float(amount),
                            entity_id=self.order_id,
                        )
                    ]
                    if amount
                    else []
                ),
            },
            "resolution_actions": actions,
        }
        return output, rule

    def verify(self, output: dict[str, Any], rule: dict[str, Any] | None) -> None:
        self.trace.contracts.validate_output(output, f"workflow/{self.case_id}")
        refs = set(output["evidence_refs"])
        owned = {e["evidence_ref"] for e in self.evidence.values()}
        if output["case_id"] != self.case_id or not refs <= owned:
            raise ValueError("Verifier rejected evidence ownership or case ID")
        for claim in output["claim_assessments"]:
            if not set(claim["evidence_refs"]) <= refs:
                raise ValueError("Verifier rejected claim linkage")
        if [claim["claim_id"] for claim in output["claim_assessments"]] != [
            claim["claim_id"] for claim in self.case["customer_request"].get("claims", [])
        ]:
            raise ValueError("Verifier rejected claim IDs")
        financial = output["financial_resolution"]
        amount = _money(financial["recommended_refund_brl"])
        if sum((_money(row["amount_brl"]) for row in financial["refund_lines"]), ZERO) != amount:
            raise ValueError("Verifier rejected refund total")
        if any(row["entity_id"] != self.order_id for row in financial["refund_lines"]):
            raise ValueError("Verifier rejected refund entity")
        fields = {
            "order_ids": "order_id",
            "item_ids": "order_item_id",
            "seller_ids": "seller_id",
            "payment_references": "payment_reference",
            "shipment_ids": "shipment_id",
        }

        def ids(value: Any, field: str) -> set[str]:
            if isinstance(value, dict):
                found = {str(value[field])} if value.get(field) is not None else set()
                for child in value.values():
                    found.update(ids(child, field))
                return found
            if isinstance(value, list):
                return set().union(*(ids(child, field) for child in value))
            return set()

        cited = [
            e["data"]
            for name, e in self.evidence.items()
            if name != "get_policy" and e["evidence_ref"] in refs
        ]
        for name, field in fields.items():
            if not set(output["affected_entities"][name]) <= ids(cited, field):
                raise ValueError("Verifier rejected entity evidence linkage")
        assessment = output["assessment"]
        parties = output["root_cause_analysis"]["responsible_parties"]
        party_types = {party["party_type"] for party in parties}
        for party in parties:
            if (
                party["party_type"] == "seller"
                and party["party_id"] not in output["affected_entities"]["seller_ids"]
            ):
                raise ValueError("Verifier rejected seller scope")
        required_party = {
            "canceled_order_paid": "platform",
            "unavailable_order_paid": "seller",
            "late_delivery_seller": "seller",
            "late_delivery_logistics": "logistics_provider",
            "duplicate_charge": "payment_provider",
            "payment_mismatch": "payment_provider",
            "refund_pending": "payment_provider",
            "refund_failed": "payment_provider",
            "valid_split_payment": "customer",
            "unsupported_claim": "customer",
        }.get(assessment["primary_issue"])
        if required_party and party_types != {required_party}:
            raise ValueError("Verifier rejected responsibility")
        if rule and (
            amount != _money(rule["refund_brl"])
            or assessment["case_status"] != rule["case_status"]
            or output["resolution_actions"] != [rule["recommended_action"]]
        ):
            raise ValueError("Verifier rejected policy inconsistency")
        if assessment["case_status"] == "no_action" and amount:
            raise ValueError("Verifier rejected no-action refund")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Collect case-scoped evidence, apply MCP policy and verify before returning."""
    flow = _CaseWorkflow(case, gateway, trace)
    flow.tools = set(await gateway.list_tools())
    order = await flow.collect("order-agent", "get_order")
    # Sequential specialists keep requests bounded and trace handoffs deterministic.
    items = await flow.collect("order-agent", "get_order_items")
    payment = await flow.collect("payment-agent", "get_payment_timeline")
    refund = None
    if any(
        c.get("topic", "").startswith("refund_") for c in case["customer_request"].get("claims", [])
    ):
        refund = await flow.collect("payment-agent", "get_refund_timeline")
    shipment = await flow.collect("shipment-agent", "get_shipment_summary")
    policy = await flow.collect("policy-agent", "get_policy")
    output, rule = flow.decide(order, items, payment, shipment, refund, policy)
    flow.emit(
        "policy_decided",
        "policy-agent",
        decision_code=output["assessment"]["primary_issue"].upper(),
        evidence_refs=output["evidence_refs"],
    )
    flow.emit("handoff", "policy-agent", target="verifier", evidence_refs=output["evidence_refs"])
    flow.verify(output, rule)
    flow.emit(
        "verification_completed",
        "verifier",
        decision_code="VALIDATED",
        evidence_refs=output["evidence_refs"],
        attributes={
            "confidence": output["assessment"]["confidence"],
            "unavailable_tools": len(flow.failures),
        },
    )
    return output
