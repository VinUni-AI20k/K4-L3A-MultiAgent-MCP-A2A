from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway


class CountingSession:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> Any:
        self.active += 1
        self.calls += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return SimpleNamespace(
                is_error=False,
                structuredContent={
                    "schema_version": "day09-mcp-evidence-v1",
                    "evidence_ref": f"ev_{self.calls:024d}",
                    "result_hash": "sha256:" + "0" * 64,
                    "domain": "order",
                    "data": {"order_id": arguments["order_id"]},
                },
            )
        finally:
            self.active -= 1


def test_gateway_shares_global_limit_across_parallel_calls() -> None:
    async def run() -> tuple[CountingSession, list[dict[str, Any]]]:
        root = Path(__file__).resolve().parents[1]
        session = CountingSession()
        gateway = EvidenceGateway(
            session, Contracts(root / "contracts" / "schemas"), asyncio.Semaphore(2)
        )
        results = await asyncio.gather(
            *(
                gateway.call("get_order", case_id=f"CASE_{index:03d}", order_id="order-1")
                for index in range(10)
            )
        )
        return session, results

    session, results = asyncio.run(run())
    assert session.calls == 10
    assert session.max_active == 2
    assert len(results) == 10
