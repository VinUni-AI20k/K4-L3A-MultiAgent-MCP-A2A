import asyncio
import json
from pathlib import Path
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

async def main():
    s = Settings.load()
    async with connect_gateway(s.mcp_endpoint, s.team_api_key, Contracts(Path('contracts/schemas'))) as g:
        r = await g._session.list_tools()
        for t in r.tools:
            print(t.name, getattr(t, 'input_schema', getattr(t, 'inputSchema', None)))
        c = json.loads(Path('inputs/L3A_CASE_021.json').read_text(encoding='utf-8'))
        for name in ['get_order', 'get_order_payments', 'get_payment_timeline', 'get_shipment_summary', 'get_policy']:
            args = {'policy_version': c['policy_version']} if name == 'get_policy' else {'order_id': c['customer_request']['claimed_order_id']}
            try:
                e = await g.call(name, case_id=c['case_id'], **args)
                print(name, json.dumps(e['data']))
            except Exception as exc:
                print(name, type(exc).__name__, str(exc))

asyncio.run(main())
