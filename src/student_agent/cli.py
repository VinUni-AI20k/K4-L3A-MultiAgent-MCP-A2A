from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .competition import open_run
from .config import Settings
from .contracts import Contracts
from .coordinator import CaseFailedError
from .ledger import check_tool_mapping
from .mcp_gateway import connect_gateway
from .policy import RULES_VERSION
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import LOCAL_RUN_ID, solve_case


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
    run = open_run(settings)
    if run.case_set_version != case_set.version:
        raise RuntimeError(
            f"active run uses case set {run.case_set_version!r} but inputs are {case_set.version!r}"
        )
    print(f"active run: {run.variant_id} / {run.case_set_version} / expires {run.expires_at}")
    endpoint = run.mcp_endpoint or settings.mcp_endpoint
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    failed: dict[str, str] = {}
    async with connect_gateway(endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        problems = check_tool_mapping(await gateway.discover())
        if problems:
            raise RuntimeError("MCP tool mapping mismatch: " + "; ".join(problems))
        for index, case_id in enumerate(case_set.case_ids, start=1):
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            try:
                output = await solve_case(case, gateway, trace)
            except CaseFailedError as exc:
                failed[case_id] = exc.reason_code
                print(f"[{index}/{len(case_set.case_ids)}] {case_id} FAILED {exc}", file=sys.stderr)
                continue
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
            assessment = output["assessment"]
            print(
                f"[{index}/{len(case_set.case_ids)}] {case_id} {assessment['primary_issue']}"
                f" / {assessment['case_status']} / refs={len(output['evidence_refs'])}",
                flush=True,
            )
        stats = gateway.stats
    report = {
        "local_run_id": LOCAL_RUN_ID,
        "rules_version": RULES_VERSION,
        "cases": len(case_set.case_ids),
        "finalized": len(case_set.case_ids) - len(failed),
        "failed": failed,
        "mcp_requests": stats["requests"],
        "mcp_retries": stats["retries"],
    }
    # Local run report, outside the submission ZIP (traces/ only ships trace.jsonl).
    (root / "traces" / "run-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    if failed:
        raise RuntimeError(f"batch incomplete: {len(failed)} case(s) failed; do not package")


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
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
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
