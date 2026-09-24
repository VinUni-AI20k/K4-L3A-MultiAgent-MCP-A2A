from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    case_id = sys.argv[1] if len(sys.argv) > 1 else "L3A_CASE_001"
    case = json.loads((root / "inputs" / f"{case_id}.json").read_text(encoding="utf-8"))
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(root / "traces" / "smoke-trace.jsonl", contracts)
    trace.path.unlink(missing_ok=True)
    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        output = await solve_case(case, gateway, trace)
    contracts.validate_output(output, "smoke output")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
