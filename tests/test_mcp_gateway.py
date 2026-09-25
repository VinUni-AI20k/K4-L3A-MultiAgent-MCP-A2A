from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from student_agent.mcp_gateway import (
    EvidenceGateway,
    MCPPermissionError,
    MCPTimeoutError,
    MCPToolError,
)


class StubContracts:
    def validate_evidence(self, value: Any, label: str) -> None:
        assert value["evidence_ref"].startswith("ev_")
        assert label.startswith("MCP tool ")


class StubSession:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def evidence_result() -> SimpleNamespace:
    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_12345678901234567890",
        "result_hash": f"sha256:{'a' * 64}",
        "domain": "order",
        "data": {"status": "delivered"},
    }
    return SimpleNamespace(isError=False, structuredContent=evidence, content=[])


def test_call_passes_case_id_and_preserves_evidence_object() -> None:
    result = evidence_result()
    session = StubSession([result])
    gateway = EvidenceGateway(session, StubContracts())  # type: ignore[arg-type]

    evidence = asyncio.run(
        gateway.call(
            "get_order", actor="order-agent", case_id="L3A_CASE_001", order_id="order-1"
        )
    )

    assert evidence is result.structuredContent
    assert session.calls == [
        ("get_order", {"case_id": "L3A_CASE_001", "order_id": "order-1"})
    ]


def test_call_rejects_tool_outside_actor_allowlist() -> None:
    session = StubSession([evidence_result()])
    gateway = EvidenceGateway(session, StubContracts())  # type: ignore[arg-type]

    with pytest.raises(MCPPermissionError):
        asyncio.run(
            gateway.call(
                "get_order_payments", actor="shipment-agent", case_id="L3A_CASE_001"
            )
        )

    assert session.calls == []


def test_call_retries_one_timeout_then_succeeds() -> None:
    session = StubSession([httpx2.ReadTimeout("slow"), evidence_result()])
    gateway = EvidenceGateway(session, StubContracts())  # type: ignore[arg-type]

    asyncio.run(gateway.call("get_order", actor="order-agent", case_id="L3A_CASE_001"))

    assert len(session.calls) == 2


def test_call_stops_after_second_timeout() -> None:
    session = StubSession([httpx2.ReadTimeout("slow"), httpx2.ReadTimeout("still slow")])
    gateway = EvidenceGateway(session, StubContracts())  # type: ignore[arg-type]

    with pytest.raises(MCPTimeoutError, match="after 2 attempt"):
        asyncio.run(gateway.call("get_order", actor="order-agent", case_id="L3A_CASE_001"))

    assert len(session.calls) == 2


def test_call_does_not_retry_tool_error() -> None:
    error = SimpleNamespace(
        isError=True,
        structuredContent=None,
        content=[SimpleNamespace(text="order not found")],
    )
    session = StubSession([error, evidence_result()])
    gateway = EvidenceGateway(session, StubContracts())  # type: ignore[arg-type]

    with pytest.raises(MCPToolError, match="order not found"):
        asyncio.run(gateway.call("get_order", actor="order-agent", case_id="L3A_CASE_001"))

    assert len(session.calls) == 1
