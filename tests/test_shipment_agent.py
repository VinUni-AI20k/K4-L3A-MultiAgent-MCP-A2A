from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from student_agent.agents.shipment_agent import (
    ShipmentAgent,
    ShipmentInvestigationResult,
)
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


@pytest.fixture
def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


@pytest.fixture
def trace_writer(tmp_path: Path, contracts: Contracts) -> TraceWriter:
    trace_path = tmp_path / "traces" / "trace.jsonl"
    return TraceWriter(trace_path, contracts)


@pytest.mark.anyio
async def test_shipment_agent_no_order_id(trace_writer: TraceWriter) -> None:
    gateway = AsyncMock()
    agent = ShipmentAgent(gateway, trace_writer)
    result = await agent.investigate(
        case_id="L3A_CASE_001",
        claimed_order_id=None,
    )
    assert isinstance(result, ShipmentInvestigationResult)
    assert result.shipment_ids == []
    assert result.primary_issue_candidate is None
    assert result.root_cause_analysis["ranked_causes"] == []


@pytest.mark.anyio
async def test_shipment_agent_late_delivery_seller(
    trace_writer: TraceWriter, contracts: Contracts
) -> None:
    gateway = AsyncMock()

    async def mock_call(tool_name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
        if tool_name == "get_shipment_summary":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_shipment_summary_001_abc1234567890",
                "result_hash": "sha256:" + "0" * 64,
                "domain": "shipment",
                "data": {
                    "shipment_id": "ship_001",
                    "seller_id": "seller_abc",
                    "shipping_limit_date": "2018-01-10T12:00:00Z",
                    "order_delivered_carrier_date": "2018-01-15T12:00:00Z",  # 5 days late
                    "order_estimated_delivery_date": "2018-01-20T12:00:00Z",
                    "order_delivered_customer_date": "2018-01-22T12:00:00Z",
                    "carrier_name": "correios_brazil",
                },
            }
        elif tool_name == "get_sellers":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_sellers_info_001_abc1234567890",
                "result_hash": "sha256:" + "1" * 64,
                "domain": "seller",
                "data": {
                    "seller_id": "seller_abc",
                    "sla_days": 2,
                },
            }
        raise ValueError(f"Unexpected tool call: {tool_name}")

    gateway.call = AsyncMock(side_effect=mock_call)

    agent = ShipmentAgent(gateway, trace_writer)
    result = await agent.investigate(
        case_id="L3A_CASE_001",
        claimed_order_id="ord_123",
        known_seller_ids=["seller_abc"],
    )

    assert result.primary_issue_candidate == "late_delivery_seller"
    assert result.sla_breached_by == "seller"
    assert "ship_001" in result.shipment_ids
    assert "seller_abc" in result.seller_ids
    assert len(result.evidence_refs) == 2

    rca = result.root_cause_analysis
    assert len(rca["ranked_causes"]) >= 1
    assert rca["ranked_causes"][0]["cause_code"] == "SELLER_DISPATCH_TIMEOUT"
    assert rca["ranked_causes"][0]["rank"] == 1
    assert rca["responsible_parties"][0]["party_type"] == "seller"
    assert rca["responsible_parties"][0]["party_id"] == "seller_abc"

    # Validate output compatibility with schema
    mock_full_output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": "L3A_CASE_001",
        "assessment": {
            "primary_issue": result.primary_issue_candidate,
            "case_status": "action_required",
            "confidence": 0.95,
        },
        "affected_entities": {
            "order_ids": ["ord_123"],
            "item_ids": [],
            "seller_ids": result.seller_ids,
            "payment_references": [],
            "shipment_ids": result.shipment_ids,
        },
        "root_cause_analysis": result.root_cause_analysis,
        "evidence_refs": result.evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["NOTIFY_SELLER_SLA_BREACH"],
    }
    contracts.validate_output(mock_full_output, "test_output")


@pytest.mark.anyio
async def test_shipment_agent_late_delivery_logistics(
    trace_writer: TraceWriter, contracts: Contracts
) -> None:
    gateway = AsyncMock()

    async def mock_call(tool_name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
        if tool_name == "get_shipment_summary":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_shipment_summary_002_abc1234567890",
                "result_hash": "sha256:" + "0" * 64,
                "domain": "shipment",
                "data": {
                    "shipment_id": "ship_002",
                    "seller_id": "seller_xyz",
                    "shipping_limit_date": "2018-01-10T12:00:00Z",
                    "order_delivered_carrier_date": "2018-01-09T12:00:00Z",  # Dispatched on time!
                    "order_estimated_delivery_date": "2018-01-15T12:00:00Z",
                    "order_delivered_customer_date": "2018-01-20T12:00:00Z",  # 5 days carrier delay!
                    "carrier_name": "logistics_partner_1",
                },
            }
        elif tool_name == "get_sellers":
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_sellers_info_002_abc1234567890",
                "result_hash": "sha256:" + "1" * 64,
                "domain": "seller",
                "data": {"seller_id": "seller_xyz"},
            }
        raise ValueError(f"Unexpected tool call: {tool_name}")

    gateway.call = AsyncMock(side_effect=mock_call)

    agent = ShipmentAgent(gateway, trace_writer)
    result = await agent.investigate(
        case_id="L3A_CASE_002",
        claimed_order_id="ord_456",
    )

    assert result.primary_issue_candidate == "late_delivery_logistics"
    assert result.sla_breached_by == "logistics"
    assert result.delay_days_logistics == 5.0
    rca = result.root_cause_analysis
    assert rca["ranked_causes"][0]["cause_code"] == "LOGISTICS_DELAY"
    assert rca["ranked_causes"][0]["rank"] == 1
    assert rca["responsible_parties"][0]["party_type"] == "logistics_provider"
    assert rca["responsible_parties"][0]["party_id"] == "logistics_partner_1"


@pytest.mark.anyio
async def test_shipment_agent_on_time(trace_writer: TraceWriter) -> None:
    gateway = AsyncMock()

    async def mock_call(tool_name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_shipment_summary_003_abc1234567890",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "shipment",
            "data": {
                "shipment_id": "ship_003",
                "seller_id": "seller_ontime",
                "shipping_limit_date": "2018-01-10T12:00:00Z",
                "order_delivered_carrier_date": "2018-01-08T12:00:00Z",
                "order_estimated_delivery_date": "2018-01-15T12:00:00Z",
                "order_delivered_customer_date": "2018-01-12T12:00:00Z",
            },
        }

    gateway.call = AsyncMock(side_effect=mock_call)

    agent = ShipmentAgent(gateway, trace_writer)
    result = await agent.investigate(
        case_id="L3A_CASE_003",
        claimed_order_id="ord_789",
    )

    assert result.primary_issue_candidate is None
    assert result.sla_breached_by is None
    assert result.root_cause_analysis["ranked_causes"] == []
    assert result.root_cause_analysis["responsible_parties"] == []
