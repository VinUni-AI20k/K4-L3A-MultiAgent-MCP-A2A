from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx2
from mcp.shared.exceptions import MCPError

import httpx

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import MCPTransportError, connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import AgentModels, solve_case

SESSION_CHUNK_SIZE = 10
CASE_RETRY_ERRORS = (
    MCPTransportError,
    MCPError,
    httpx2.TransportError,
    TimeoutError,
    ConnectionError,
)


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path, *, as_json: bool = False) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        if as_json:
            print(json.dumps(await gateway.describe_tools(), ensure_ascii=False, indent=2))
        else:
            for tool in await gateway.list_tools():
                print(tool)


async def _start_competition_run(settings: Settings, case_set: CaseSet) -> None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{settings.competition_api_url}/api/v2/runs",
                headers={"Authorization": f"Bearer {settings.team_api_key}"},
                json={"variant_id": case_set.variant_id},
            )
            response.raise_for_status()
            run = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError("could not create the competition run") from exc
    if (
        run.get("variant_id") != case_set.variant_id
        or run.get("case_set_version") != case_set.version
    ):
        raise RuntimeError("competition run does not match the local case-set")
    print(
        f"RUN: {run['variant_id']} / {run['case_set_version']} / "
        f"expires {run.get('expires_at', 'unknown')}",
        flush=True,
    )

async def _run_session_chunk(
    settings: Settings,
    contracts: Contracts,
    chunk: list[tuple[str, dict[str, Any]]],
    progress: list[int],
    received_case_ids: set[str],
    output_root: Path,
    trace: TraceWriter,
) -> None:
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        while progress[0] < len(chunk):
            case_id, raw_case = chunk[progress[0]]
            case = dict(raw_case)
            if case_id not in received_case_ids:
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                received_case_ids.add(case_id)
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            if not output.get("evidence_refs"):
                raise RuntimeError(f"solver returned no auditable evidence for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            progress[0] += 1


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

    case_items = [(case_id, case_set.cases[case_id]) for case_id in case_set.case_ids]
    received_case_ids: set[str] = set()
    for chunk_start in range(0, len(case_items), SESSION_CHUNK_SIZE):
        chunk = case_items[chunk_start : chunk_start + SESSION_CHUNK_SIZE]
        progress = [0]
        consecutive_failures = 0
        while progress[0] < len(chunk):
            previous_progress = progress[0]
            try:
                await _run_session_chunk(
                    settings,
                    contracts,
                    chunk,
                    progress,
                    received_case_ids,
                    output_root,
                    trace,
                )
            except CASE_RETRY_ERRORS as exc:
                if progress[0] > previous_progress:
                    consecutive_failures = 0
                consecutive_failures += 1
                case_id = chunk[progress[0]][0]
                if consecutive_failures >= 3:
                    raise RuntimeError(
                        f"MCP case {case_id} failed after {consecutive_failures} attempts"
                    ) from exc
                await asyncio.sleep(float(2 ** (consecutive_failures - 1)))
            else:
                break


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true",
        help="skip contract-valid cases that already have a case_finalized trace event",
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
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root, as_json=args.json))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
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
