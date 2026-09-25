from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.mcp_gateway import EvidenceGateway, is_retryable_error


class FakeContracts:
    def __init__(self) -> None:
        self.validated: list[dict[str, Any]] = []

    def validate_evidence(self, value: dict[str, Any], label: str) -> None:
        self.validated.append(value)


class FakeSession:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> Any:
        return self.result


@pytest.mark.anyio
async def test_gateway_accepts_mcp_v2_python_field_names() -> None:
    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_payment_reference_000001",
        "result_hash": "sha256:" + ("0" * 64),
        "domain": "payment",
        "data": [],
    }
    result = SimpleNamespace(
        is_error=False,
        structured_content=evidence,
        content=[],
    )
    contracts = FakeContracts()
    gateway = EvidenceGateway(FakeSession(result), contracts)  # type: ignore[arg-type]

    returned = await gateway.call(
        "get_order_payments", case_id="CASE_001", order_id="ORDER-001"
    )

    assert returned == evidence
    assert contracts.validated == [evidence]


@pytest.mark.anyio
async def test_gateway_raises_for_mcp_v2_error_result() -> None:
    result = SimpleNamespace(
        is_error=True,
        structured_content=None,
        content=[SimpleNamespace(text="not found")],
    )
    gateway = EvidenceGateway(FakeSession(result), FakeContracts())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="not found"):
        await gateway.call(
            "get_order_payments", case_id="CASE_001", order_id="ORDER-001"
        )


def test_retryable_error_finds_transport_error_inside_exception_group() -> None:
    error = ExceptionGroup("MCP task group failed", [ConnectionError("connection reset")])

    assert is_retryable_error(error)


def test_retryable_error_rejects_contract_errors() -> None:
    assert not is_retryable_error(ValueError("invalid evidence"))
