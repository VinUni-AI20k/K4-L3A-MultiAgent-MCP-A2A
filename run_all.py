import asyncio
import json
import logging
from pathlib import Path
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.cases import load_case_set
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

logging.basicConfig(level=logging.INFO)

async def main():
    root = Path('.').resolve()
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Xóa sạch để chạy fresh từ đầu
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)
    
    print("Bắt đầu xử lý 100 cases với kết nối an toàn...")
    for idx, case_id in enumerate(case_set.case_ids, 1):
        target = output_root / f"{case_id}.json"
        case = case_set.cases[case_id]
        
        # Thử lại tối đa 5 lần cho mỗi case nếu mạng chập chờn
        success = False
        for attempt in range(1, 6):
            try:
                async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case, gw, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    target.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    success = True
                    break
            except Exception as e:
                print(f"[{case_id}] Thử lần {attempt} gặp lỗi: {e}. Đang thử lại...")
                await asyncio.sleep(1.5 * attempt)
                
        if not success:
            raise RuntimeError(f"Không thể hoàn thành case {case_id} sau 5 lần thử.")
            
        if idx % 10 == 0 or idx == 100:
            print(f"--> Đã hoàn tất: {idx}/100 cases.")

if __name__ == "__main__":
    asyncio.run(main())
