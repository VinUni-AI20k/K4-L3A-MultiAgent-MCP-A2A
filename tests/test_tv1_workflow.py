"""Tests cho phần TV1: tích hợp cả nhóm qua coordinator, evidence rules, adjudicator, guard.

Dữ liệu dưới đây là TỔNG HỢP (không phải payload thi đấu) nhưng có cùng cấu trúc với
response MCP thật, kể cả bản ghi "nhiễu" nằm ngoài dòng thời gian của case.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from student_agent.agents import adjudicator
from student_agent.agents.base import EvidenceLedger
from student_agent.agents.guard import enforce_invariants, fallback_output
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
CASE_ID = "L3A_CASE_900"
ORDER = "order-900"
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
REQUIRED_ARGS = {tool: {"order_id"} for tool in TOOL_DOMAINS}
REQUIRED_ARGS["get_policy"] = {"policy_version"}
REQUIRED_ARGS["get_customer_history"] = {"customer_unique_id"}


def _rule(status: str, action: str, refund: float, party: str, pid: str | None = None) -> dict:
    return {
        "case_status": status,
        "recommended_action": action,
        "refund_brl": refund,
        "responsible_parties": [{"party_id": pid, "party_type": party}],
    }


POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "canceled_order_paid": _rule("action_required", "issue_refund", 60.0, "platform"),
        "unavailable_order_paid": _rule(
            "action_required", "issue_refund", 60.0, "seller", "seller-example"
        ),
        "late_delivery_seller": _rule(
            "action_required", "refund_freight", 10.0, "seller", "seller-example"
        ),
        "late_delivery_logistics": _rule(
            "action_required", "refund_freight", 10.0, "logistics_provider"
        ),
        "duplicate_charge": _rule(
            "action_required", "refund_duplicate_charge", 60.0, "payment_provider"
        ),
        "payment_mismatch": _rule(
            "action_required", "reconcile_payment", 20.0, "payment_provider"
        ),
        "refund_failed": _rule("action_required", "retry_refund", 60.0, "payment_provider"),
        "refund_pending": _rule(
            "needs_investigation", "monitor_refund", 0.0, "payment_provider"
        ),
        "valid_split_payment": _rule("no_action", "document_no_action", 0.0, "customer"),
        "unsupported_claim": _rule("no_action", "document_no_action", 0.0, "customer"),
    },
}


def ts(day: str) -> str:
    return f"2018-{day}T09:00:00-03:00"


def base_data() -> dict[str, Any]:
    """Đơn giao đúng hạn, trả 1 lần 60.00; kèm 1 item + 1 payment nhiễu ngoài cửa sổ."""
    return {
        "get_order": {
            "order_id": ORDER,
            "customer_id": "customer-900",
            "order_status": "delivered",
            "order_purchase_timestamp": ts("03-01"),
            "order_delivered_carrier_date": ts("03-03"),
            "order_delivered_customer_date": ts("03-08"),
            "order_estimated_delivery_date": ts("03-10"),
        },
        "get_order_items": [
            {"order_id": ORDER, "order_item_id": "item-A", "product_id": "p-A",
             "seller_id": "seller-A", "shipping_limit_date": ts("03-04"),
             "price": "50.00", "freight_value": "10.00"},
            {"order_id": ORDER, "order_item_id": "item-A", "product_id": "p-A",
             "seller_id": "seller-A", "shipping_limit_date": ts("07-01"),
             "price": "50.00", "freight_value": "7.00"},
        ],
        "get_order_payments": [
            {"order_id": ORDER, "payment_sequential": "1", "payment_type": "credit_card",
             "payment_installments": "1", "payment_value": "60.00"},
            {"order_id": ORDER, "payment_sequential": "1", "payment_type": "credit_card",
             "payment_installments": "1", "payment_value": "7.00"},
        ],
        "get_payment_timeline": {
            "order_id": ORDER,
            "payments": [],
            "events": [
                {"event_at": ts("03-01"), "event_type": "captured", "amount_brl": "60.00",
                 "status": "confirmed"},
                {"event_at": ts("06-28"), "event_type": "captured", "amount_brl": "7.00",
                 "status": "confirmed"},
            ],
        },
        "get_shipment_summary": {
            "order_id": ORDER,
            "order_status": "delivered",
            "delivered_carrier_at": ts("03-03"),
            "delivered_customer_at": ts("03-08"),
            "estimated_delivery_at": ts("03-10"),
            "shipping_limits": [
                {"order_item_id": "item-A", "seller_id": "seller-A",
                 "shipping_limit_at": ts("03-04")},
                {"order_item_id": "item-A", "seller_id": "seller-A",
                 "shipping_limit_at": ts("07-01")},
            ],
            "events": [],
        },
        "get_sellers": [{"seller_id": "seller-A", "seller_state": "SP"}],
        "get_product_context": [{"product_id": "p-A"}],
        "get_policy": POLICY,
    }


def make_case(topic: str) -> dict[str, Any]:
    return {
        "case_id": CASE_ID,
        "opened_at": ts("03-15"),
        "customer_request": {
            "language": "vi",
            "message": "test",
            "claimed_order_id": ORDER,
            "claims": [
                {"claim_id": "claim-900-a", "topic": topic},
                {"claim_id": "claim-900-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


class FakeGateway:
    """Giả lập MCP: tool thiếu dữ liệu hoặc sai tham số → lỗi như server thật."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data if data is not None else base_data()
        self.calls: list[str] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append(tool_name)
        if set(arguments) != REQUIRED_ARGS[tool_name] or tool_name not in self.data:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": ref(f"{case_id}:{tool_name}"),
            "result_hash": "sha256:" + "0" * 64,
            "domain": TOOL_DOMAINS[tool_name],
            "data": copy.deepcopy(self.data[tool_name]),
            "warnings": [],
        }


def ref(label: str) -> str:
    return "ev_" + hashlib.sha256(label.encode()).hexdigest()[:24]


def run(tmp_path: Path, data: dict[str, Any], topic: str) -> tuple[dict, list[dict]]:
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    output = asyncio.run(solve_case(make_case(topic), FakeGateway(data), trace))
    CONTRACTS.validate_output(output, "output")
    events = [json.loads(line) for line in trace.path.read_text("utf-8").splitlines()]
    consumed = {
        r for e in events if e["event_type"] == "tool_result_consumed"
        for r in e.get("evidence_refs") or []
    }
    assert set(output["evidence_refs"]) <= consumed  # evidence-to-trace linkage
    kinds = {e["event_type"] for e in events}
    assert {"task_assigned", "handoff", "policy_decided", "verification_completed"} <= kinds
    return output, events


def domains(output: dict[str, Any]) -> set[str]:
    by_ref = {ref(f"{CASE_ID}:{t}"): d for t, d in TOOL_DOMAINS.items()}
    return {by_ref[r] for r in output["evidence_refs"]}


# ── End-to-end theo từng loại issue ───────────────────────────────────────


def test_canceled_order_ignores_out_of_window_late_event(tmp_path: Path) -> None:
    data = base_data()
    data["get_order"]["order_status"] = "canceled"
    data["get_shipment_summary"]["order_status"] = "canceled"
    data["get_shipment_summary"]["events"] = [
        {"event_at": ts("07-10"), "event_type": "delivered_late", "actor": "seller",
         "status": "confirmed"}
    ]
    out, _ = run(tmp_path, data, "canceled_order_paid")
    assert out["assessment"]["primary_issue"] == "canceled_order_paid"
    assert out["assessment"]["case_status"] == "action_required"
    assert out["financial_resolution"]["recommended_refund_brl"] == 60.0
    assert out["resolution_actions"] == ["issue_refund"]
    assert {"order", "payment", "policy"} <= domains(out)
    assert "customer" not in domains(out) and "product" not in domains(out)
    assert out["affected_entities"]["order_ids"] == [ORDER]


def test_late_delivery_seller_uses_case_seller_not_policy_example(tmp_path: Path) -> None:
    data = base_data()
    data["get_shipment_summary"]["delivered_carrier_at"] = ts("03-06")
    data["get_shipment_summary"]["delivered_customer_at"] = ts("03-12")
    out, _ = run(tmp_path, data, "late_delivery_seller")
    assert out["assessment"]["primary_issue"] == "late_delivery_seller"
    parties = out["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "seller", "party_id": "seller-A"}]
    assert out["affected_entities"]["seller_ids"] == ["seller-A"]
    assert out["financial_resolution"]["recommended_refund_brl"] == 10.0


def test_late_delivery_logistics_when_seller_handed_over_on_time(tmp_path: Path) -> None:
    data = base_data()
    data["get_shipment_summary"]["delivered_customer_at"] = ts("03-12")
    out, _ = run(tmp_path, data, "late_delivery_seller")  # khách khai sai bên có lỗi
    assert out["assessment"]["primary_issue"] == "late_delivery_logistics"


def test_split_payment_ignores_stale_failed_refund(tmp_path: Path) -> None:
    data = base_data()
    data["get_payment_timeline"]["events"][0]["amount_brl"] = "30.00"
    data["get_payment_timeline"]["events"].insert(
        1, {"event_at": ts("03-01"), "event_type": "captured", "amount_brl": "30.00",
            "status": "confirmed"})
    data["get_refund_timeline"] = {"order_id": ORDER, "events": [
        {"event_at": "2017-12-01T09:00:00-03:00", "event_type": "refund_requested",
         "amount_brl": "30.00", "status": "failed"}]}
    out, _ = run(tmp_path, data, "valid_split_payment")
    assert out["assessment"]["primary_issue"] == "valid_split_payment"
    assert out["assessment"]["case_status"] == "no_action"
    assert out["financial_resolution"]["recommended_refund_brl"] == 0
    assert any("refund" in c["field"] for c in out["data_conflicts"])


def test_duplicate_charge_when_repeated_capture_exceeds_order_total(tmp_path: Path) -> None:
    data = base_data()
    data["get_payment_timeline"]["events"].insert(
        1, {"event_at": ts("03-01"), "event_type": "captured", "amount_brl": "60.00",
            "status": "confirmed"})
    out, _ = run(tmp_path, data, "duplicate_charge")
    assert out["assessment"]["primary_issue"] == "duplicate_charge"


def test_refund_pending_in_window(tmp_path: Path) -> None:
    data = base_data()
    data["get_refund_timeline"] = {"order_id": ORDER, "events": [
        {"event_at": ts("03-14"), "event_type": "refund_requested", "amount_brl": "60.00",
         "status": "pending"}]}
    out, _ = run(tmp_path, data, "refund_pending")
    assert out["assessment"]["primary_issue"] == "refund_pending"
    assert out["assessment"]["case_status"] == "needs_investigation"
    assert "refund" in domains(out)


def test_unsupported_claim_when_nothing_wrong_in_window(tmp_path: Path) -> None:
    data = base_data()
    data["get_shipment_summary"]["events"] = [
        {"event_at": ts("07-10"), "event_type": "delivered_late",
         "actor": "logistics_provider", "status": "confirmed"}
    ]
    out, _ = run(tmp_path, data, "late_delivery_logistics")
    assert out["assessment"]["primary_issue"] == "unsupported_claim"
    assert out["assessment"]["case_status"] == "no_action"
    verdicts = {c["claim_id"]: c["verdict"] for c in out["claim_assessments"]}
    assert verdicts["claim-900-a"] == "unsupported"


def test_all_tools_failing_gives_valid_insufficient_output(tmp_path: Path) -> None:
    out, _ = run(tmp_path, {}, "canceled_order_paid")
    assert out["assessment"]["primary_issue"] == "insufficient_evidence"
    assert out["evidence_refs"] == []


# ── Real EvidenceGateway với MCP session giả (mcp>=2 dùng is_error) ────────


class _FakeSession:
    def __init__(self, result: CallToolResult) -> None:
        self.result = result

    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        return self.result


def test_gateway_reads_mcp2_results() -> None:
    envelope = asyncio.run(FakeGateway().call("get_order", case_id=CASE_ID, order_id=ORDER))
    ok = CallToolResult(content=[], structured_content=envelope)
    gateway = EvidenceGateway(_FakeSession(ok), CONTRACTS)
    assert asyncio.run(gateway.call("get_order", case_id=CASE_ID)) == envelope

    failed = CallToolResult(content=[TextContent(type="text", text="not found")], is_error=True)
    gateway = EvidenceGateway(_FakeSession(failed), CONTRACTS)
    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(gateway.call("get_order", case_id=CASE_ID))


# ── Adjudicator ───────────────────────────────────────────────────────────


def _ledger(*pairs: tuple[str, str]) -> EvidenceLedger:
    ledger = EvidenceLedger(CASE_ID)
    for label, domain in pairs:
        ledger.add({"evidence_ref": ref(label), "domain": domain}, label, "test")
    return ledger


CLAIMS = make_case("late_delivery_seller")["customer_request"]["claims"]


def test_claim_hypothesis_breaks_near_tie() -> None:
    ledger = _ledger(("o", "order"), ("p", "payment"), ("s", "shipment"))
    results = {
        "payment-agent": {"issues": [{"issue": "payment_mismatch", "strength": 0.8}]},
        "shipment-agent": {"issues": [{"issue": "late_delivery_seller", "strength": 0.75}]},
    }
    decision = adjudicator.decide(CLAIMS, results, ledger)
    assert decision.primary_issue == "late_delivery_seller"
    assert decision.confidence < 0.85  # hai ứng viên sát nhau → giảm confidence


def test_no_signal_is_insufficient_not_claim() -> None:
    decision = adjudicator.decide(CLAIMS, {}, _ledger())
    assert decision.primary_issue == "insufficient_evidence"
    assert decision.case_status == "needs_investigation"


def test_malformed_signals_are_ignored() -> None:
    results = {"x": {"issues": [{"issue": "made_up"}, "junk",
                                {"issue": "refund_failed", "strength": "high"}]}}
    decision = adjudicator.decide([], results, _ledger(("o", "order")))
    assert decision.primary_issue == "insufficient_evidence"


# ── Guard ─────────────────────────────────────────────────────────────────


def test_guard_no_action_means_no_refund_and_drops_foreign_refs() -> None:
    ledger = _ledger(("o", "order"))
    case = make_case("valid_split_payment")
    draft = fallback_output(case, ledger)
    draft["assessment"] = {"primary_issue": "valid_split_payment", "case_status": "no_action",
                           "confidence": 1.7}
    draft["evidence_refs"] = [ref("o"), ref("other-case")]
    draft["financial_resolution"]["refund_lines"] = [
        {"reason_code": "x", "amount_brl": 9.99, "entity_id": None}
    ]
    draft["resolution_actions"] = ["notify customer", "notify customer", " "]
    out, fixes = enforce_invariants(draft, case, ledger)
    CONTRACTS.validate_output(out, "guard")
    assert out["evidence_refs"] == [ref("o")]
    assert out["financial_resolution"]["recommended_refund_brl"] == 0
    assert out["assessment"]["confidence"] == 1.0
    assert out["resolution_actions"] == ["notify customer"]
    assert "refund_without_action" in fixes
    assert enforce_invariants(out, case, ledger)[0] == out  # idempotent


def test_transient_mcp_error_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.shared.exceptions import MCPError

    from student_agent.agents import base

    monkeypatch.setattr(base, "RETRY_BACKOFF_SECONDS", 0)
    gateway = FakeGateway()
    real_call, failures = gateway.call, {"get_policy": 1}

    async def flaky(tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if failures.get(tool_name):
            failures[tool_name] -= 1
            raise MCPError(code=-32603, message="502 Bad Gateway")
        return await real_call(tool_name, case_id=case_id, **arguments)

    gateway.call = flaky  # type: ignore[method-assign]
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    out = asyncio.run(solve_case(make_case("unsupported_claim"), gateway, trace))
    assert out["assessment"]["case_status"] == "no_action"
    assert out["resolution_actions"] == ["document_no_action"]


def test_dead_transport_is_not_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.shared.exceptions import MCPError

    from student_agent.agents import base

    monkeypatch.setattr(base, "RETRY_BACKOFF_SECONDS", 0)
    gateway = FakeGateway()

    async def dead(tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        raise MCPError(code=-32000, message="Connection closed")

    gateway.call = dead  # type: ignore[method-assign]
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    with pytest.raises(base.TransportLost):
        asyncio.run(solve_case(make_case("unsupported_claim"), gateway, trace))


def test_cli_reconnects_and_redoes_only_the_interrupted_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib
    import shutil

    from student_agent import cli
    from student_agent.agents import base

    monkeypatch.setattr(base, "RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(cli, "RECONNECT_DELAY_SECONDS", 0)
    for name in ("contracts",):
        shutil.copytree(ROOT / name, tmp_path / name)
    ids = ["L3A_CASE_901", "L3A_CASE_902"]
    (tmp_path / "inputs").mkdir()
    for cid in ids:
        case = make_case("unsupported_claim")
        case["case_id"] = cid
        (tmp_path / "inputs" / f"{cid}.json").write_text(json.dumps(case), "utf-8")
    (tmp_path / "case-set.json").write_text(json.dumps(
        {"case_set_version": "t", "variant_id": "l3a", "case_ids": ids}), "utf-8")
    monkeypatch.setattr(cli, "load_case_set", lambda root: __import__(
        "student_agent.cases", fromlist=["load_case_set"]).load_case_set(root, expected_count=2))
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls, root: cls(
        "http://x", "sk-team-" + "a" * 20, "http://x/mcp", root)))
    sessions = {"n": 0}

    @contextlib.asynccontextmanager
    async def fake_connect(endpoint: str, key: str, contracts: Contracts):  # type: ignore[no-untyped-def]
        sessions["n"] += 1
        gateway = FakeGateway()
        if sessions["n"] == 1:  # phiên đầu chết giữa case thứ 2
            real = gateway.call

            async def flaky(tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
                if case_id == "L3A_CASE_902" and tool_name == "get_payment_timeline":
                    raise MCPError(code=-32000, message="Connection closed")
                return await real(tool_name, case_id=case_id, **arguments)

            gateway.call = flaky  # type: ignore[method-assign]
        gateway.list_tools = lambda: asyncio.sleep(0, result=["get_order"])  # type: ignore[attr-defined]
        yield gateway

    from mcp.shared.exceptions import MCPError

    monkeypatch.setattr(cli, "connect_gateway", fake_connect)
    asyncio.run(cli._run(tmp_path))
    assert sessions["n"] == 2
    events = [json.loads(x) for x in (tmp_path / "traces" / "trace.jsonl").read_text("utf-8")
              .splitlines()]
    received = [e["case_id"] for e in events if e["event_type"] == "case_received"]
    assert received == ids  # case 902 chỉ còn 1 lần chạy (lần dở đã bị cắt khỏi trace)
    for cid in ids:
        out = json.loads((tmp_path / "outputs" / f"{cid}.json").read_text("utf-8"))
        assert out["assessment"]["primary_issue"] == "unsupported_claim"
