from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        return self.responses[tool_name]


def evidence(reference: str, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{reference}",
        "result_hash": "sha256:" + ("0" * 64),
        "domain": domain,
        "data": data,
    }


def test_workflow_builds_contract_valid_output(tmp_path: Path) -> None:
    order_id = "order-001"
    responses = {
        "get_order": evidence(
            "order_reference_00000001",
            "order",
            {"order_id": order_id, "order_status": "canceled"},
        ),
        "get_order_items": evidence(
            "items_reference_00000001",
            "item",
            [
                {
                    "order_id": order_id,
                    "order_item_id": 1,
                    "seller_id": "seller-001",
                    "price": 100.0,
                    "freight_value": 10.0,
                }
            ],
        ),
        "get_shipment_summary": evidence(
            "shipment_reference_00001",
            "shipment",
            {"order_id": order_id},
        ),
        "get_sellers": evidence(
            "seller_reference_0000001",
            "seller",
            [{"seller_id": "seller-001"}],
        ),
        "get_order_payments": evidence(
            "payments_reference_000001",
            "payment",
            [{"payment_reference": "payment-001", "payment_value": 110.0}],
        ),
        "get_payment_timeline": evidence(
            "timeline_reference_00001",
            "payment",
            [{"payment_status": "captured", "payment_reference": "payment-001"}],
        ),
        "get_refund_timeline": evidence(
            "refund_reference_000001",
            "refund",
            [],
        ),
        "get_policy": evidence(
            "policy_reference_000001",
            "policy",
            {"policy_version": "EC_POLICY_V1"},
        ),
    }
    case = {
        "case_id": "L3A_CASE_TEST",
        "customer_request": {
            "claimed_order_id": order_id,
            "claims": [
                {"claim_id": "claim-a", "topic": "canceled_order_paid"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")

    output = asyncio.run(solve_case(case, FakeGateway(responses), trace))

    contracts.validate_output(output, "workflow output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 110.0
    assert output["evidence_refs"]
    trace_lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"event_type":"verification_completed"' in line for line in trace_lines)
