"""Tests cho phần TV1: ledger, adjudicator, guard, coordinator end-to-end (gateway giả)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from student_agent.agents import adjudicator
from student_agent.agents.base import BaseAgent, EvidenceLedger
from student_agent.agents.guard import enforce_invariants, fallback_output
from student_agent.agents.order_agent import OrderAgent
from student_agent.agents.payment_agent import PaymentAgent
from student_agent.agents.shipment_agent import ShipmentAgent
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
    "get_product_context": "product",
    "get_customer_history": "customer",
}
CASE = {
    "case_id": "L3A_CASE_900",
    "opened_at": "2018-01-01T09:00:00-03:00",
    "customer_request": {
        "language": "vi",
        "message": "test",
        "claimed_order_id": "order-1",
        "claims": [
            {"claim_id": "claim-900-a", "topic": "late_delivery_seller"},
            {"claim_id": "claim-900-b", "topic": "requested_full_refund"},
        ],
    },
    "policy_version": "EC_POLICY_V1",
}


def ref(label: str) -> str:
    return "ev_" + hashlib.sha256(label.encode()).hexdigest()[:24]


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id))
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": ref(f"{case_id}:{tool_name}"),
            "result_hash": "sha256:" + "0" * 64,
            "domain": TOOL_DOMAINS[tool_name],
            "data": {},
        }


def run_case(tmp_path: Path, case: dict[str, Any] = CASE) -> tuple[dict, list[dict]]:
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    output = asyncio.run(solve_case(case, FakeGateway(), trace))
    CONTRACTS.validate_output(output, "output")
    events = [json.loads(line) for line in trace.path.read_text("utf-8").splitlines()]
    return output, events


# ── End-to-end ────────────────────────────────────────────────────────────


def test_skeleton_agents_give_valid_insufficient_output(tmp_path: Path) -> None:
    output, events = run_case(tmp_path)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    kinds = {e["event_type"] for e in events}
    assert {"task_assigned", "handoff", "policy_decided", "verification_completed"} <= kinds


def test_signals_drive_decision_and_evidence_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def order_run(self: BaseAgent, case_id: str, ctx: dict) -> dict:
        await self.call_tool("get_order", case_id, order_id=ctx["order_id"])
        await self.call_tool("get_customer_history", case_id, customer_unique_id="c1")
        self.emit_handoff(case_id, target="coordinator")
        return {"order_ids": [ctx["order_id"]], "seller_ids": ["seller-1"], "issues": []}

    async def ship_run(self: BaseAgent, case_id: str, ctx: dict) -> dict:
        ship = await self.call_tool("get_shipment_summary", case_id, order_id=ctx["order_id"])
        sell = await self.call_tool("get_sellers", case_id, order_id=ctx["order_id"])
        self.emit_handoff(case_id, target="coordinator")
        refs = [ship["evidence_ref"], sell["evidence_ref"], "ev_forged_ref_not_from_mcp_000"]
        return {
            "shipment_ids": ["ship-1"],
            "issues": [self.signal("late_delivery_seller", 0.85, refs)],
        }

    async def pay_run(self: BaseAgent, case_id: str, ctx: dict) -> dict:
        pay = await self.call_tool("get_order_payments", case_id, order_id=ctx["order_id"])
        self.emit_handoff(case_id, target="coordinator")
        return {
            "payment_references": ["order-1:1"],
            "issues": [self.signal("valid_split_payment", 0.4, [pay["evidence_ref"]])],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": 0,
                "refund_lines": [
                    {"reason_code": "late_delivery", "amount_brl": 10.005, "entity_id": "o"},
                    {"reason_code": "shipping_fee", "amount_brl": 5.1, "entity_id": "o"},
                ],
            },
        }

    monkeypatch.setattr(OrderAgent, "run", order_run)
    monkeypatch.setattr(ShipmentAgent, "run", ship_run)
    monkeypatch.setattr(PaymentAgent, "run", pay_run)
    output, events = run_case(tmp_path)

    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["assessment"]["confidence"] >= 0.8
    cid = CASE["case_id"]
    # Đúng domain: order + shipment + seller; không có customer/payment/ref giả.
    assert set(output["evidence_refs"]) == {
        ref(f"{cid}:get_order"), ref(f"{cid}:get_shipment_summary"), ref(f"{cid}:get_sellers")
    }
    # Lỗi seller → responsible_parties có seller.
    parties = output["root_cause_analysis"]["responsible_parties"]
    assert {"party_type": "seller", "party_id": "seller-1"} in parties
    # Tiền: Decimal làm tròn, tổng khớp.
    fin = output["financial_resolution"]
    assert fin["recommended_refund_brl"] == 15.11
    verdicts = {c["claim_id"]: c["verdict"] for c in output["claim_assessments"]}
    assert verdicts == {"claim-900-a": "supported", "claim-900-b": "supported"}
    # Mọi ref trong output đều xuất hiện trong trace tool_result_consumed của case.
    consumed = {
        r for e in events if e["event_type"] == "tool_result_consumed"
        for r in e["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed


def test_crashing_specialist_does_not_break_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: BaseAgent, case_id: str, ctx: dict) -> dict:
        raise KeyError("data shape changed")

    monkeypatch.setattr(OrderAgent, "run", boom)
    output, events = run_case(tmp_path)
    assert output["case_id"] == CASE["case_id"]
    assert any(e.get("decision_code") == "agent_error" for e in events)


# ── Real EvidenceGateway với MCP session giả (mcp>=2 dùng is_error) ────────


class _FakeSession:
    def __init__(self, result: CallToolResult) -> None:
        self.result = result

    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        return self.result


def test_gateway_reads_mcp2_results() -> None:
    envelope = asyncio.run(FakeGateway().call("get_order", case_id="L3A_CASE_900"))
    ok = CallToolResult(content=[], structured_content=envelope)
    gateway = EvidenceGateway(_FakeSession(ok), CONTRACTS)
    assert asyncio.run(gateway.call("get_order", case_id="L3A_CASE_900")) == envelope

    failed = CallToolResult(content=[TextContent(type="text", text="not found")], is_error=True)
    gateway = EvidenceGateway(_FakeSession(failed), CONTRACTS)
    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(gateway.call("get_order", case_id="L3A_CASE_900"))


# ── Adjudicator ───────────────────────────────────────────────────────────


def _ledger(*pairs: tuple[str, str]) -> EvidenceLedger:
    ledger = EvidenceLedger("L3A_CASE_900")
    for label, domain in pairs:
        ledger.add({"evidence_ref": ref(label), "domain": domain}, label, "test")
    return ledger


def test_claim_hypothesis_breaks_near_tie() -> None:
    ledger = _ledger(("o", "order"), ("p", "payment"), ("s", "shipment"))
    results = {
        "payment-agent": {"issues": [{"issue": "payment_mismatch", "strength": 0.8}]},
        "shipment-agent": {"issues": [{"issue": "late_delivery_seller", "strength": 0.75}]},
    }
    decision = adjudicator.decide(CASE["customer_request"]["claims"], results, ledger)
    assert decision.primary_issue == "late_delivery_seller"
    assert decision.confidence < 0.85  # hai ứng viên sát nhau → giảm confidence


def test_no_signal_is_insufficient_not_claim() -> None:
    decision = adjudicator.decide(CASE["customer_request"]["claims"], {}, _ledger())
    assert decision.primary_issue == "insufficient_evidence"
    assert decision.case_status == "needs_investigation"


def test_malformed_signals_are_ignored() -> None:
    results = {"x": {"issues": [{"issue": "made_up"}, "junk", {"issue": "refund_failed",
                                                               "strength": "high"}]}}
    decision = adjudicator.decide([], results, _ledger(("o", "order")))
    assert decision.primary_issue == "insufficient_evidence"


# ── Guard ─────────────────────────────────────────────────────────────────


def test_guard_no_action_means_no_refund_and_drops_foreign_refs() -> None:
    ledger = _ledger(("o", "order"))
    draft = fallback_output(CASE, ledger)
    draft["assessment"] = {"primary_issue": "valid_split_payment", "case_status": "no_action",
                           "confidence": 1.7}
    draft["evidence_refs"] = [ref("o"), ref("other-case")]
    draft["financial_resolution"]["refund_lines"] = [
        {"reason_code": "x", "amount_brl": 9.99, "entity_id": None}
    ]
    draft["resolution_actions"] = ["notify customer", "notify customer", " "]
    out, fixes = enforce_invariants(draft, CASE, ledger)
    CONTRACTS.validate_output(out, "guard")
    assert out["evidence_refs"] == [ref("o")]
    assert out["financial_resolution"]["recommended_refund_brl"] == 0
    assert out["assessment"]["confidence"] == 1.0
    assert out["resolution_actions"] == ["notify customer"]
    assert "refund_without_action" in fixes
    assert enforce_invariants(out, CASE, ledger)[0] == out  # idempotent
