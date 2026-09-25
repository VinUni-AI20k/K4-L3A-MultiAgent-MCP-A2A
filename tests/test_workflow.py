"""Offline workflow tests. Fixture evidence refs are fake and never leave the tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.a2a import ORDER_AGENT, AgentResult, EntityIds
from student_agent.contracts import Contracts
from student_agent.coordinator import CaseFailedError, analyze_intake
from student_agent.mcp_gateway import ToolCallError
from student_agent.trace import TraceWriter
from student_agent.workflow import build_coordinator

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
ORDER = "e2a03ccf5ea816036608b2d8c3ab8e60"
DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_policy": "policy",
}


class FakeGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.stats = {"requests": 0, "retries": 0}

    async def call(
        self, tool_name: str, *, case_id: str, deadline: float | None = None, **arguments: str
    ):
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if tool_name not in self.data:
            raise ToolCallError("EVIDENCE_NOT_FOUND", tool_name)
        digest = hashlib.sha256(f"{case_id}{tool_name}".encode()).hexdigest()
        envelope = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_test_{digest[:24]}",
            "result_hash": f"sha256:{digest}",
            "domain": DOMAINS[tool_name],
            "data": self.data[tool_name],
        }
        CONTRACTS.validate_evidence(envelope)
        return envelope


def case(topic: str = "canceled_order_paid") -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_001",
        "customer_request": {
            "language": "vi",
            "message": "Đơn hàng có dấu hiệu bất thường sau thanh toán.",
            "claimed_order_id": ORDER,
            "claims": [
                {"claim_id": "claim-001-a", "topic": topic},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


def run(gateway: FakeGateway, tmp_path: Path, topic: str = "canceled_order_paid"):
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    trace.emit(case_id="L3A_CASE_001", event_type="case_received", actor="coordinator")
    output = asyncio.run(build_coordinator(gateway, CONTRACTS).solve(case(topic), trace))
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    return output, events


P = "2017-12-20T09:00:00-03:00"


def capture(at: str, amount: str) -> dict[str, str]:
    return {"event_at": at, "event_type": "captured", "amount_brl": amount, "status": "confirmed"}


CANCELED = {
    "get_order": {
        "order_id": ORDER,
        "order_status": "canceled",
        "order_purchase_timestamp": P,
        "order_delivered_customer_date": None,
        "order_estimated_delivery_date": "2017-12-30T09:00:00-03:00",
    },
    "get_order_items": [
        {
            "order_id": ORDER,
            "order_item_id": "item-1",
            "seller_id": "seller-1",
            "shipping_limit_date": "2017-12-23T09:00:00-03:00",
            "price": "79.00",
            "freight_value": "10.00",
        },
        # row from another purchase window: must be ignored
        {
            "order_id": ORDER,
            "order_item_id": "item-1",
            "seller_id": "seller-1",
            "shipping_limit_date": "2018-05-14T09:00:00-03:00",
            "price": "79.00",
            "freight_value": "18.00",
        },
    ],
    "get_payment_timeline": {
        "order_id": ORDER,
        "payments": [
            {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "79.00"}
        ],
        "events": [
            capture("2017-12-20T10:00:00-03:00", "79.00"),
            capture("2018-05-11T10:00:00-03:00", "18.00"),
        ],
    },
    "get_shipment_summary": {
        "order_status": "canceled",
        "delivered_customer_at": None,
        "estimated_delivery_at": "2017-12-30T09:00:00-03:00",
        "shipping_limits": [],
        "events": [],
    },
    "get_policy": {"policy_version": "EC_POLICY_V1", "rules": {}},
}


def test_intake_routes_by_claim_topic() -> None:
    intake = analyze_intake(case("late_delivery_seller"))
    assert intake.lookup_ids == EntityIds(order_ids=(ORDER,))
    assert intake.routes == ("order-agent", "payment-agent", "shipment-agent")
    assert intake.policy_version == "EC_POLICY_V1"


def test_canceled_paid_order_gets_full_refund(tmp_path: Path) -> None:
    gateway = FakeGateway(CANCELED)
    output, events = run(gateway, tmp_path)
    CONTRACTS.validate_output(output, "output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["affected_entities"]["seller_ids"] == ["seller-1"]
    assert all(call[1]["case_id"] == "L3A_CASE_001" for call in gateway.calls)
    consumed = {
        r for e in events if e["event_type"] == "tool_result_consumed" for r in e["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    types = [e["event_type"] for e in events]
    for required in (
        "case_received",
        "task_assigned",
        "handoff",
        "policy_decided",
        "verification_completed",
    ):
        assert required in types
    assert "case_finalized" not in types  # the CLI emits it after writing the output


def test_all_tools_failing_fails_the_case_instead_of_writing_output(tmp_path: Path) -> None:
    with pytest.raises(CaseFailedError, match="NO_EVIDENCE_COLLECTED"):
        run(FakeGateway({}), tmp_path)


def test_late_delivery_blames_seller_when_handoff_was_late(tmp_path: Path) -> None:
    data = dict(CANCELED)
    data["get_order"] = {**CANCELED["get_order"], "order_status": "delivered"}
    data["get_payment_timeline"] = {"events": [capture("2017-12-20T10:00:00-03:00", "18.00")]}
    data["get_shipment_summary"] = {
        "order_status": "delivered",
        "delivered_customer_at": "2018-01-05T09:00:00-03:00",
        "estimated_delivery_at": "2018-01-01T09:00:00-03:00",
        "events": [
            {
                "event_at": "2018-01-05T09:00:00-03:00",
                "event_type": "delivered_late",
                "actor": "seller",
                "status": "confirmed",
            },
        ],
    }
    output, _ = run(FakeGateway(data), tmp_path, "late_delivery_seller")
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["financial_resolution"]["recommended_refund_brl"] == 10.0  # freight
    assert {p["party_type"] for p in output["root_cause_analysis"]["responsible_parties"]} == {
        "seller"
    }


def test_invalid_specialist_result_is_rejected(tmp_path: Path) -> None:
    coordinator = build_coordinator(FakeGateway(CANCELED), CONTRACTS)

    class Liar:
        actor = ORDER_AGENT

        async def handle(self, task, context):
            return AgentResult(
                task.local_run_id, "OTHER_CASE", task.task_id, self.actor, "completed", None
            )

    coordinator._agents = {**coordinator._agents, ORDER_AGENT: Liar()}
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    trace.emit(case_id="L3A_CASE_001", event_type="case_received", actor="coordinator")
    output = asyncio.run(coordinator.solve(case(), trace))
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("decision_code") == "INVALID_SPECIALIST_RESULT" for e in events)
    # Without order evidence the refund cannot be justified from payment data alone.
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_verifier_rejection_fails_the_case(tmp_path: Path) -> None:
    coordinator = build_coordinator(FakeGateway(CANCELED), CONTRACTS)
    verifier = coordinator._agents["verifier"]
    verifier.check = lambda *args: (["REFUND_TOTAL_MISMATCH"], True)
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    trace.emit(case_id="L3A_CASE_001", event_type="case_received", actor="coordinator")
    with pytest.raises(CaseFailedError, match="REMEDIATION_FAILED"):
        asyncio.run(coordinator.solve(case(), trace))
