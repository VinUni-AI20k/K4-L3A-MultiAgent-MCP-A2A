from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    # Process cases in batches with concurrent execution
    batch_size = 20
    concurrency = 3  # cases at a time within a batch
    all_case_ids = list(case_set.case_ids)

    async def _process_one(case_id: str, gateway, sem: asyncio.Semaphore) -> None:
        async with sem:
            target = output_root / f"{case_id}.json"
            if target.exists():
                return
            case = case_set.cases[case_id]
            trace.emit(
                case_id=case_id,
                event_type="case_received",
                actor="coordinator",
            )
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(
                    f"solver returned a mismatched case_id for {case_id}"
                )
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(target)
            trace.emit(
                case_id=case_id,
                event_type="case_finalized",
                actor="coordinator",
            )

    import time
    start_total = time.time()
    for batch_start in range(0, len(all_case_ids), batch_size):
        batch = all_case_ids[batch_start : batch_start + batch_size]
        batch_end = min(batch_start + batch_size, len(all_case_ids))
        print(f"[{batch_start + 1}-{batch_end}/{len(all_case_ids)}] Processing...", flush=True)
        t_batch = time.time()
        max_connection_retries = 3
        for conn_attempt in range(max_connection_retries):
            try:
                async with connect_gateway(
                    settings.mcp_endpoint, settings.team_api_key, contracts
                ) as gateway:
                    discovered_tools = await gateway.list_tools()
                    if not discovered_tools:
                        raise RuntimeError("MCP Gateway returned no tools")
                    sem = asyncio.Semaphore(concurrency)
                    tasks = [_process_one(cid, gateway, sem) for cid in batch]
                    await asyncio.gather(*tasks)
                print(f"[{batch_end}/{len(all_case_ids)}] Done ({time.time() - t_batch:.1f}s)", flush=True)
                break  # Batch succeeded
            except Exception as exc:
                print(
                    f"Connection error on batch starting at {batch_start} "
                    f"(attempt {conn_attempt + 1}/{max_connection_retries}): {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                if conn_attempt < max_connection_retries - 1:
                    await asyncio.sleep(2 * (conn_attempt + 1))
                else:
                    raise
    print(f"All {len(all_case_ids)} cases completed in {time.time() - start_total:.1f}s", flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
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
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
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
