from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from student_agent import cli
from student_agent.cases import CaseSet
from student_agent.config import Settings
from student_agent.mcp_gateway import ToolFailure


def test_failed_run_preserves_existing_outputs_and_trace(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "contracts", tmp_path / "contracts")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "traces").mkdir()
    original_output = tmp_path / "outputs/CASE_001.json"
    original_trace = tmp_path / "traces/trace.jsonl"
    original_output.write_text('{"previous":true}\n')
    original_trace.write_text("previous trace\n")
    case = {
        "case_id": "CASE_001",
        "policy_version": "EC_POLICY_V1",
        "opened_at": "2018-01-10T12:00:00-03:00",
        "customer_request": {"claimed_order_id": "order"},
    }
    cases = CaseSet("test-v1", "l3a", ("CASE_001",), {"CASE_001": case})
    monkeypatch.setattr(cli, "load_case_set", lambda _: cases)
    settings = Settings(
        "https://example.test", "unit-test-secret", "https://example.test/mcp", tmp_path
    )
    monkeypatch.setattr(cli.Settings, "load", lambda _: settings)

    class Response:
        is_success = True

        def json(self):
            return {
                "variant_id": "l3a",
                "case_set_version": "test-v1",
                "mcp_endpoint": settings.mcp_endpoint,
                "expires_at": "test",
            }

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return Response()

    class BrokenGateway:
        async def list_tools(self):
            return ["get_order"]

        async def call(self, *args, **kwargs):
            raise ToolFailure("outage")

    @asynccontextmanager
    async def connection(*args):
        yield BrokenGateway()

    monkeypatch.setattr(cli.httpx2, "AsyncClient", Client)
    monkeypatch.setattr(cli, "connect_gateway", connection)
    with pytest.raises(ExceptionGroup):
        asyncio.run(cli._run(tmp_path))
    assert original_output.read_text() == '{"previous":true}\n'
    assert original_trace.read_text() == "previous trace\n"
