from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

import student_agent.mcp_gateway as mcp_gateway
from student_agent.contracts import ContractError, Contracts
from student_agent.mcp_gateway import EvidenceGateway


class ConcurrencySession:
    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        self.active = 0
        self.max_active = 0

    async def call_tool(
        self,
        name: str,
        *,
        arguments: dict[str, Any],
        read_timeout_seconds: float | None = None,
    ) -> object:
        del name, arguments, read_timeout_seconds
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return SimpleNamespace(is_error=False, structured_content=self.evidence, content=[])


def _evidence(data: dict[str, Any], *, digest: str | None = None) -> dict[str, Any]:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result_hash = digest or hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_abcdefghijklmnopqrstuvwxyz",
        "result_hash": f"sha256:{result_hash}",
        "domain": "order",
        "data": data,
    }


class Session:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        name: str,
        *,
        arguments: dict[str, Any],
        read_timeout_seconds: float | None = None,
    ) -> object:
        del read_timeout_seconds
        self.calls.append((name, arguments))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_gateway_supports_current_mcp_result_and_retries_transient_errors() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    evidence = _evidence({"order_id": "ORDER-1", "order_status": "canceled"})
    result = SimpleNamespace(is_error=False, structured_content=evidence, content=[])
    session = Session(
        [httpx2.ReadTimeout("temporary timeout"), httpx2.ReadTimeout("another timeout"), result]
    )
    gateway = EvidenceGateway(
        session, contracts, max_attempts=3, retry_backoff_seconds=0  # type: ignore[arg-type]
    )

    actual = asyncio.run(
        gateway.call("get_order", case_id="CASE_001", order_id="ORDER-1")
    )

    assert actual == evidence
    assert len(session.calls) == 3
    assert all(arguments["case_id"] == "CASE_001" for _, arguments in session.calls)


def test_gateway_serializes_transport_calls_during_concurrent_agent_fanout() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    evidence = _evidence({"order_id": "ORDER-1", "order_status": "canceled"})
    session = ConcurrencySession(evidence)
    gateway = EvidenceGateway(session, contracts, retry_backoff_seconds=0)  # type: ignore[arg-type]

    async def run() -> None:
        await asyncio.gather(
            gateway.call("get_order", case_id="CASE_001", order_id="ORDER-1"),
            gateway.call("get_order_items", case_id="CASE_001", order_id="ORDER-1"),
        )

    asyncio.run(run())
    assert session.max_active == 1


def test_gateway_rejects_result_hash_mismatch_without_retry() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    evidence = _evidence({"order_id": "ORDER-1"}, digest="0" * 64)
    result = SimpleNamespace(is_error=False, structured_content=evidence, content=[])
    session = Session([result])
    gateway = EvidenceGateway(session, contracts, retry_backoff_seconds=0)  # type: ignore[arg-type]

    with pytest.raises(ContractError, match="result_hash"):
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="ORDER-1"))

    assert len(session.calls) == 1


@pytest.mark.parametrize("error_code", [CONNECTION_CLOSED, REQUEST_TIMEOUT])
def test_gateway_recreates_session_for_transient_mcp_errors(error_code: int) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    evidence = _evidence({"order_id": "ORDER-1", "order_status": "canceled"})
    result = SimpleNamespace(is_error=False, structured_content=evidence, content=[])
    sessions = [
        Session([MCPError(code=error_code, message="transient MCP failure")]),
        Session([result]),
    ]
    entered = 0

    @asynccontextmanager
    async def session_factory() -> Any:
        nonlocal entered
        session = sessions[entered]
        entered += 1
        yield session

    async def run() -> dict[str, Any]:
        gateway = mcp_gateway.EvidenceGateway(
            None,
            contracts,
            session_factory=session_factory,
            retry_backoff_seconds=0,
        )
        await gateway.start()
        try:
            return await gateway.call(
                "get_order", case_id="CASE_001", order_id="ORDER-1"
            )
        finally:
            await gateway.close()

    actual = asyncio.run(run())
    assert actual == evidence
    assert entered == 2
    assert len(sessions[0].calls) == 1
    assert len(sessions[1].calls) == 1
    assert sessions[1].calls[0][1]["case_id"] == "CASE_001"


def test_connect_gateway_retries_session_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    attempts = 0

    @asynccontextmanager
    async def fake_connected_session(endpoint: str, team_api_key: str) -> Any:
        nonlocal attempts
        del endpoint, team_api_key
        attempts += 1
        if attempts < 3:
            raise httpx2.ConnectTimeout("temporary connect timeout")
        yield object()

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(mcp_gateway, "_connected_session", fake_connected_session)
    monkeypatch.setattr(mcp_gateway.asyncio, "sleep", no_sleep)

    async def connect() -> None:
        async with mcp_gateway.connect_gateway("https://mcp.invalid", "key", contracts):
            pass

    asyncio.run(connect())
    assert attempts == 3


def test_connect_gateway_retries_generic_initialize_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    attempts = 0

    @asynccontextmanager
    async def fake_connected_session(endpoint: str, team_api_key: str) -> Any:
        nonlocal attempts
        del endpoint, team_api_key
        attempts += 1
        if attempts < 3:
            raise MCPError(code=-32000, message="upstream returned 502")
        yield object()

    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(mcp_gateway, "_connected_session", fake_connected_session)
    monkeypatch.setattr(mcp_gateway.asyncio, "sleep", no_sleep)

    async def connect() -> None:
        async with mcp_gateway.connect_gateway("https://mcp.invalid", "key", contracts):
            pass

    asyncio.run(connect())
    assert attempts == 3


def test_connect_gateway_suppresses_internal_close_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")

    @asynccontextmanager
    async def fake_connected_session(endpoint: str, team_api_key: str) -> Any:
        del endpoint, team_api_key
        yield object()
        raise asyncio.CancelledError

    monkeypatch.setattr(mcp_gateway, "_connected_session", fake_connected_session)

    async def connect() -> None:
        async with mcp_gateway.connect_gateway("https://mcp.invalid", "key", contracts):
            pass

    asyncio.run(connect())


def test_connect_gateway_does_not_retry_errors_from_case_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    attempts = 0

    @asynccontextmanager
    async def fake_connected_session(endpoint: str, team_api_key: str) -> Any:
        nonlocal attempts
        del endpoint, team_api_key
        attempts += 1
        yield object()

    monkeypatch.setattr(mcp_gateway, "_connected_session", fake_connected_session)

    async def fail_in_workflow() -> None:
        async with mcp_gateway.connect_gateway("https://mcp.invalid", "key", contracts):
            raise ValueError("case failed")

    with pytest.raises(ValueError, match="case failed"):
        asyncio.run(fail_in_workflow())
    assert attempts == 1
