from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agents.base import TransportLost
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

    # The MCP transport can drop mid-run (TLS ConnectError / ReadError / 502). That kills
    # the whole session, so reconnect and redo ONLY the interrupted case: its partial
    # trace lines are truncated first, so every case's evidence comes from one session.
    pending = list(case_set.case_ids)
    failures = 0
    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                if not await gateway.list_tools():
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending:
                    case_id = pending[0]
                    mark = trace_path.stat().st_size if trace_path.exists() else 0
                    try:
                        await _solve_one(case_set.cases[case_id], gateway, trace, contracts,
                                         output_root)
                    except BaseException:
                        _truncate(trace_path, mark)
                        raise
                    pending.pop(0)
                    failures = 0
        except (Exception, TransportLost) as exc:  # noqa: BLE001 — connection lost
            failures += 1
            if failures > MAX_RECONNECTS:
                raise
            print(
                f"WARN: MCP connection lost at {pending[0]} ({type(exc).__name__}); "
                f"reconnecting ({failures}/{MAX_RECONNECTS})",
                file=sys.stderr,
            )
            await asyncio.sleep(RECONNECT_DELAY_SECONDS * failures)


MAX_RECONNECTS = 5
RECONNECT_DELAY_SECONDS = 5.0


def _truncate(path: Path, size: int) -> None:
    if path.exists() and path.stat().st_size > size:
        with path.open("r+b") as handle:
            handle.truncate(size)


async def _solve_one(
    case: dict, gateway: object, trace: TraceWriter, contracts: Contracts, output_root: Path
) -> None:
    case_id = case["case_id"]
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
