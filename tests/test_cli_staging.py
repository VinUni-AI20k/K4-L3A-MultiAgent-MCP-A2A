from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from student_agent import cli
from student_agent.cases import CaseSet
from student_agent.config import Settings
from student_agent.contracts import Contracts


class FakeGateway:
    async def list_tools(self) -> list[str]:
        return ["get_order"]


def test_failed_run_preserves_previous_outputs_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = "TEST_CASE_001"
    case_set = CaseSet(
        version="test-v1",
        variant_id="l3a",
        case_ids=(case_id,),
        cases={case_id: {"case_id": case_id}},
    )
    settings = Settings(
        "https://example.test", "sk-team-test-key-12345678", "https://mcp.test", tmp_path
    )
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    previous_output = tmp_path / "outputs" / f"{case_id}.json"
    previous_trace = tmp_path / "traces" / "trace.jsonl"
    previous_output.parent.mkdir(parents=True)
    previous_trace.parent.mkdir(parents=True)
    previous_output.write_text('{"previous":"output"}\n', encoding="utf-8")
    previous_trace.write_text('{"previous":"trace"}\n', encoding="utf-8")

    monkeypatch.setattr(cli.Settings, "load", lambda root: settings)
    monkeypatch.setattr(cli, "load_case_set", lambda root: case_set)
    monkeypatch.setattr(cli, "Contracts", lambda schema_root: contracts)

    @asynccontextmanager
    async def fake_connect_gateway(endpoint: str, key: str, schema: Contracts, limiter: Any):
        yield FakeGateway()

    async def fail_solve(case: dict[str, Any], gateway: Any, trace: Any) -> dict[str, Any]:
        raise RuntimeError("simulated MCP failure")

    monkeypatch.setattr(cli, "connect_gateway", fake_connect_gateway)
    monkeypatch.setattr(cli, "solve_case", fail_solve)

    with pytest.raises(RuntimeError, match="simulated MCP failure"):
        asyncio.run(cli._run(tmp_path, concurrency=1))

    assert previous_output.read_text(encoding="utf-8") == '{"previous":"output"}\n'
    assert previous_trace.read_text(encoding="utf-8") == '{"previous":"trace"}\n'
    assert not list((tmp_path / "dist").glob(".day09-run-*"))
