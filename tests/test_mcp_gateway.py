from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.mcp_gateway import EvidenceGateway


class FakeContracts:
    def __init__(self) -> None:
        self.validated: list[tuple[dict[str, Any], str]] = []

    def validate_evidence(self, value: dict[str, Any], label: str) -> None:
        self.validated.append((value, label))


class FakeSession:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.list_calls = 0

    async def list_tools(self) -> Any:
        self.list_calls += 1
        return SimpleNamespace(tools=[SimpleNamespace(name="get_order")])

    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> Any:
        self.calls.append((tool_name, arguments))
        return self.result


def test_gateway_uses_case_id_and_validates_structured_evidence() -> None:
    evidence = {"schema_version": "day09-mcp-evidence-v1", "data": {"ok": True}}
    result = SimpleNamespace(is_error=False, structured_content=evidence, content=[])
    session = FakeSession(result)
    contracts = FakeContracts()
    gateway = EvidenceGateway(session, contracts)  # type: ignore[arg-type]

    actual = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="order-1"))

    assert actual is evidence
    assert session.calls == [
        ("get_order", {"case_id": "CASE_001", "order_id": "order-1"})
    ]
    assert contracts.validated == [(evidence, "MCP tool get_order")]
    assert session.list_calls == 1


def test_gateway_surfaces_snake_case_tool_error() -> None:
    result = SimpleNamespace(
        is_error=True,
        structured_content=None,
        content=[SimpleNamespace(text="backend unavailable")],
    )
    gateway = EvidenceGateway(FakeSession(result), FakeContracts())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="backend unavailable"):
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="order-1"))


def test_gateway_rejects_undiscovered_tool_without_calling_it() -> None:
    session = FakeSession(SimpleNamespace())
    gateway = EvidenceGateway(session, FakeContracts())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="not available"):
        asyncio.run(gateway.call("made_up_tool", case_id="CASE_001"))

    assert session.calls == []
