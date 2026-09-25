from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import EvidenceGateway, connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.describe_tools():
            print(json.dumps(tool, ensure_ascii=False))


async def _run(root: Path, concurrency: int) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    stage_parent = root / "dist"
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage = tempfile.TemporaryDirectory(prefix=".day09-run-", dir=stage_parent)
    stage_root = Path(stage.name)
    output_root = stage_root / "outputs"
    trace_path = stage_root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(trace_path, contracts)
    # Ten cases can be scheduled together; bound the gateway to the endpoint's
    # observed stable limit of two in-flight tool requests.
    tool_limiter = asyncio.Semaphore(min(concurrency, 2))
    completed = 0

    async def process(case_id: str, gateway: EvidenceGateway) -> None:
        nonlocal completed
        case = case_set.cases[case_id]
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, trace)
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
        target = output_root / f"{case_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        completed += 1
        if completed % 10 == 0:
            print(f"Completed {completed}/{len(case_set.case_ids)} cases", flush=True)

    async def worker(case_ids: list[str]) -> None:
        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts, tool_limiter
        ) as gateway:
            if not await gateway.list_tools():
                raise RuntimeError("MCP Gateway returned no tools")
            for case_id in case_ids:
                await process(case_id, gateway)

    try:
        worker_count = min(concurrency, len(case_set.case_ids))
        assignments = [case_set.case_ids[index::worker_count] for index in range(worker_count)]
        await asyncio.gather(*(worker(case_ids) for case_ids in assignments))
        staged_outputs, trace_lines = validate_artifacts(stage_root, case_set, contracts)
        no_evidence = sum(not output["evidence_refs"] for output in staged_outputs.values())
        tool_error_cases = {
            event["case_id"]
            for line in trace_lines
            if (event := json.loads(line)).get("decision_code")
            in {"tool_result_unavailable", "EVIDENCE_UNAVAILABLE", "POLICY_UNAVAILABLE"}
        }
        if (
            no_evidence > len(case_set.case_ids) // 10
            or len(tool_error_cases) > len(case_set.case_ids) // 5
        ):
            raise RuntimeError(
                f"MCP evidence unhealthy: {no_evidence} cases without refs, "
                f"{len(tool_error_cases)} cases with tool errors; previous artifacts preserved"
            )

        final_outputs = root / "outputs"
        final_trace = root / "traces" / "trace.jsonl"
        final_outputs.mkdir(parents=True, exist_ok=True)
        final_trace.parent.mkdir(parents=True, exist_ok=True)
        for stale in final_outputs.glob("*.json"):
            stale.unlink()
        for output in output_root.glob("*.json"):
            output.replace(final_outputs / output.name)
        trace_path.replace(final_trace)
    finally:
        stage.cleanup()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--concurrency", type=int, default=1, help="parallel case workers (default: 1)"
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            if args.concurrency < 1 or args.concurrency > 10:
                raise ValueError("--concurrency must be between 1 and 10")
            asyncio.run(_run(root, args.concurrency))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
