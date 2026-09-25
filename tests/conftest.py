from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.agents import CaseState
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]

# Same shape as the real get_policy payload; values are illustrative.
POLICY_DATA: dict[str, Any] = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_id": None, "party_type": "platform"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-e58fb7bfd033", "party_type": "seller"}],
        },
        "refund_pending": {
            "case_status": "needs_investigation",
            "recommended_action": "monitor_refund",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "unsupported_claim": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    },
}

TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_customer_history": "customer",
    "get_product_context": "product",
    "get_policy": "policy",
}

# Tool ownership from TASK_ASSIGNMENT.md (one owning actor per real MCP tool).
TOOL_ACTORS = {
    "get_order": "order-agent",
    "get_order_items": "order-agent",
    "get_product_context": "order-agent",
    "get_customer_history": "order-agent",
    "get_shipment_summary": "shipment-agent",
    "get_sellers": "shipment-agent",
    "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
    "get_policy": "policy-agent",
}


class FakeGateway:
    """Offline stand-in for EvidenceGateway that returns contract-valid evidence envelopes."""

    def __init__(self, contracts: Contracts, data: dict[str, Any] | None = None) -> None:
        self.contracts = contracts
        self.data = {"get_policy": POLICY_DATA, **(data or {})}
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if tool_name not in TOOL_DOMAINS:
            raise RuntimeError(f"MCP tool {tool_name} failed: unknown tool")
        self.calls.append((tool_name, case_id, arguments))
        evidence = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{case_id}_{tool_name}_{len(self.calls):04d}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": TOOL_DOMAINS[tool_name],
            "data": self.data.get(tool_name, {}),
            "warnings": [],
        }
        self.contracts.validate_evidence(evidence)
        return evidence


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(ROOT / "contracts" / "schemas")


@pytest.fixture
def trace(tmp_path: Path, contracts: Contracts) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", contracts)


@pytest.fixture
def gateway(contracts: Contracts) -> FakeGateway:
    return FakeGateway(contracts)


def read_trace(trace: TraceWriter) -> list[dict[str, Any]]:
    if not trace.path.exists():
        return []
    return [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]


def make_state(case_id: str = "L3A_CASE_001", topics: tuple[str, ...] = ()) -> CaseState:
    claims = [
        {"claim_id": f"claim-{case_id[-3:]}-{chr(97 + index)}", "topic": topic}
        for index, topic in enumerate(topics)
    ]
    return CaseState.from_case(
        {
            "case_id": case_id,
            "opened_at": "2018-01-01T09:00:00-03:00",
            "customer_request": {
                "language": "vi",
                "message": "test",
                "claimed_order_id": "e2a03ccf5ea816036608b2d8c3ab8e60",
                "claims": claims,
            },
            "policy_version": "EC_POLICY_V1",
        }
    )
