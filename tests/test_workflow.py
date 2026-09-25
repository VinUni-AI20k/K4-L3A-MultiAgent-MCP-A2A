from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent import payment, workflow
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "fedcba9876543210fedcba9876543210"
SELLER = "seller-fedcba987654"
_refs = itertools.count()


class FakeGateway:
    """Serves canned MCP evidence; unknown tools fail like the real gateway."""

    def __init__(self, data: dict[str, tuple[str, Any]]) -> None:
        self.data = data
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return sorted(self.data)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        if tool_name not in self.data:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool")
        domain, data = self.data[tool_name]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_wf{case_id[-3:]}{next(_refs):020d}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": domain,
            "data": data,
        }


def canceled_order_data() -> dict[str, tuple[str, Any]]:
    order = {
        "order_id": ORDER_ID, "customer_id": "customer-row-1", "order_status": "canceled",
        "order_purchase_timestamp": "2018-02-28T09:00:00-03:00",
        "order_approved_at": "2018-02-28T10:00:00-03:00",
        "order_delivered_carrier_date": "2018-03-02T09:00:00-03:00",
        "order_delivered_customer_date": None,
        "order_estimated_delivery_date": "2018-03-10T09:00:00-03:00",
    }
    item = {"order_id": ORDER_ID, "order_item_id": "item-1", "product_id": "product-1",
            "seller_id": SELLER, "shipping_limit_date": "2018-03-03T09:00:00-03:00",
            "price": "79.00", "freight_value": "10.00"}
    payment_row = {"order_id": ORDER_ID, "payment_sequential": "1",
                   "payment_type": "credit_card", "payment_installments": "1",
                   "payment_value": "79.00"}
    rule = {"case_status": "action_required", "recommended_action": "issue_refund",
            "refund_brl": 79.0, "responsible_parties": [{"party_type": "platform",
                                                         "party_id": None}]}
    return {
        "get_order": ("order", order),
        "get_order_items": ("item", [item]),
        "get_product_context": ("product", [{"product_id": "product-1"}]),
        "get_order_payments": ("payment", [payment_row]),
        "get_payment_timeline": ("payment", {"order_id": ORDER_ID, "payments": [payment_row],
                                             "events": [{"event_at": "2018-02-28T10:00:00-03:00",
                                                         "event_type": "captured",
                                                         "amount_brl": "79.00",
                                                         "status": "confirmed"}]}),
        "get_shipment_summary": ("shipment", {
            "order_id": ORDER_ID, "order_status": "canceled",
            "delivered_carrier_at": order["order_delivered_carrier_date"],
            "delivered_customer_at": None,
            "estimated_delivery_at": order["order_estimated_delivery_date"],
            "shipping_limits": [], "events": []}),
        "get_sellers": ("seller", [{"seller_id": SELLER}]),
        "get_policy": ("policy", {"rules": {"canceled_order_paid": rule}}),
    }


def make_case(case_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "opened_at": "2018-03-12T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [{"claim_id": "c-a", "topic": "canceled_order_paid"},
                       {"claim_id": "c-b", "topic": "requested_full_refund"}],
        },
        "policy_version": "EC_POLICY_V1",
    }


@pytest.fixture(autouse=True)
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_llm(*_: Any, **__: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(payment, "analyze_payment_with_llm", fake_llm)


def test_solve_case_end_to_end(tmp_path: Path) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = FakeGateway(canceled_order_data())

    outputs = [asyncio.run(workflow.solve_case(make_case(cid), gateway, trace))
               for cid in ("L3A_CASE_801", "L3A_CASE_802")]

    for output in outputs:
        contracts.validate_output(output, "workflow output")
        assert output["assessment"]["primary_issue"] == "canceled_order_paid"
        assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert not set(outputs[0]["evidence_refs"]) & set(outputs[1]["evidence_refs"])
    assert ("get_product_context", {"order_id": ORDER_ID}) in gateway.calls

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    first = [e for e in events if e["case_id"] == "L3A_CASE_801"]
    types = [e["event_type"] for e in first]
    for required in ("task_assigned", "handoff", "tool_result_consumed",
                     "verification_completed"):
        assert required in types
    assert types.index("task_assigned") < types.index("tool_result_consumed")
    assert {e["actor"] for e in first} >= {
        "coordinator", "order-agent", "payment_agent", "shipment_policy_agent", "verifier"
    }
    traced = {ref for e in first for ref in e.get("evidence_refs", [])}
    assert set(outputs[0]["evidence_refs"]) <= traced


def test_failing_specialist_does_not_sink_the_case(tmp_path: Path) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    class Broken(FakeGateway):
        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
            if tool_name == "get_shipment_summary":
                raise ConnectionError("network down")
            return await super().call(tool_name, case_id=case_id, **arguments)

    output = asyncio.run(
        workflow.solve_case(make_case("L3A_CASE_803"), Broken(canceled_order_data()), trace)
    )
    contracts.validate_output(output, "workflow output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"


def test_gateway_marks_session_lost_and_fails_fast() -> None:
    from student_agent.mcp_gateway import EvidenceGateway, SessionLostError

    class DeadSession:
        calls = 0

        async def call_tool(self, *_: Any, **__: Any) -> Any:
            DeadSession.calls += 1
            raise ConnectionError("session expired")

    gateway = EvidenceGateway(DeadSession(), Contracts(ROOT / "contracts" / "schemas"))  # type: ignore[arg-type]
    for _ in range(2):
        with pytest.raises(SessionLostError):
            asyncio.run(gateway.call("get_order", case_id="L3A_CASE_804", order_id=ORDER_ID))
    assert gateway.session_lost
    assert DeadSession.calls == 1  # second call fails fast without touching the session
