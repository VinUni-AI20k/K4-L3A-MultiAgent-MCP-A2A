from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway, ToolFailure

ROOT = Path(__file__).resolve().parents[1]


class Session:
    def __init__(self):
        self.list_calls = 0
        self.calls = []
        self.fail = False

    async def list_tools(self, *, params=None):
        self.list_calls += 1
        schema = {
            "type": "object",
            "required": ["case_id", "order_id"],
            "properties": {"case_id": {"type": "string"}, "order_id": {"type": "string"}},
        }
        tool = SimpleNamespace(name="get_order", input_schema=schema)
        return SimpleNamespace(tools=[tool], next_cursor=None)

    async def call_tool(self, name, *, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            is_error=self.fail,
            content=[],
            structured_content={
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_UNIT_TEST_ONLY_abcdefghijklmnop",
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": {"order_id": arguments["order_id"]},
            },
        )


def test_discovery_cached_but_not_evidence():
    async def scenario():
        session = Session()
        gateway = EvidenceGateway(session, Contracts(ROOT / "contracts/schemas"))
        await gateway.call("get_order", case_id="CASE_001", order_id="order")
        await gateway.call("get_order", case_id="CASE_001", order_id="order")
        assert session.list_calls == 1
        assert len(session.calls) == 2

    asyncio.run(scenario())


def test_undiscovered_tool_never_sent_to_server():
    async def scenario():
        session = Session()
        gateway = EvidenceGateway(session, Contracts(ROOT / "contracts/schemas"))
        with pytest.raises(ValueError, match="not in discovered"):
            await gateway.call("invented_tool", case_id="CASE_001", order_id="order")
        assert not session.calls

    asyncio.run(scenario())


def test_input_schema_enforced_before_network_call():
    async def scenario():
        session = Session()
        gateway = EvidenceGateway(session, Contracts(ROOT / "contracts/schemas"))
        with pytest.raises(ValidationError):
            await gateway.call("get_order", case_id="CASE_001")
        assert not session.calls

    asyncio.run(scenario())


def test_server_error_not_converted_to_empty_data():
    async def scenario():
        session = Session()
        session.fail = True
        gateway = EvidenceGateway(session, Contracts(ROOT / "contracts/schemas"))
        with pytest.raises(ToolFailure):
            await gateway.call("get_order", case_id="CASE_001", order_id="order")
        assert not gateway.evidence_calls

    asyncio.run(scenario())


def test_same_ref_across_cases_rejected():
    async def scenario():
        gateway = EvidenceGateway(Session(), Contracts(ROOT / "contracts/schemas"))
        await gateway.call("get_order", case_id="CASE_001", order_id="order")
        with pytest.raises(ValueError, match="reused across"):
            await gateway.call("get_order", case_id="CASE_002", order_id="order")

    asyncio.run(scenario())
