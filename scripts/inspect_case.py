from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    case_id_arg = sys.argv[1] if len(sys.argv) > 1 else "L3A_CASE_001"
    case = json.loads(
        (root / "inputs" / f"{case_id_arg}.json").read_text(encoding="utf-8")
    )
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]
    calls = {
        "get_order": {"order_id": order_id},
        "get_order_items": {"order_id": order_id},
        "get_order_payments": {"order_id": order_id},
        "get_payment_timeline": {"order_id": order_id},
        "get_refund_timeline": {"order_id": order_id},
        "get_shipment_summary": {"order_id": order_id},
        "get_sellers": {"order_id": order_id},
        "get_policy": {"policy_version": case["policy_version"]},
    }
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for name, arguments in calls.items():
            try:
                result = await gateway.call(name, case_id=case_id, **arguments)
            except RuntimeError as exc:
                print(json.dumps({name: {"error": str(exc)}}, ensure_ascii=False, indent=2))
            else:
                print(json.dumps({name: result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
