from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx2

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
    # Competition Workspace starts/renews a team run before evidence calls.
    # This does not upload a submission or consume a submission slot.
    async with httpx2.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            settings.competition_api_url + "/api/v2/runs",
            headers={"Authorization": f"Bearer {settings.team_api_key}"},
            json={"variant_id": "l3a"},
        )
    if not response.is_success:
        raise RuntimeError(f"Cannot start L3A competition run (HTTP {response.status_code})")
    run = response.json()
    if run.get("variant_id") != "l3a" or run.get("case_set_version") != case_set.version:
        raise ValueError("Competition run does not match the local case set")
    if run.get("mcp_endpoint") != settings.mcp_endpoint:
        raise ValueError("Run MCP endpoint differs from MCP_ENDPOINT; update local configuration")
    print(f"L3A run ready; expires_at={run.get('expires_at')}", flush=True)
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    extra = {path.stem for path in output_root.glob("*.json")} - set(case_set.case_ids)
    if extra:
        raise ValueError(f"Existing outputs outside this case-set: {sorted(extra)}")
    # An outage must not replace a previously valid run with 100 invented fallbacks.
    with tempfile.TemporaryDirectory(prefix="day09-run-") as temp:
        staging = Path(temp)
        (staging / "outputs").mkdir()
        trace = TraceWriter(staging / "traces" / "trace.jsonl", contracts)
        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts
        ) as gateway:
            if not await gateway.list_tools():
                raise RuntimeError("MCP Gateway returned no tools")
            semaphore = asyncio.Semaphore(2)
            completed = 0

            async def run_one(case_id: str) -> None:
                nonlocal completed
                async with semaphore:
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case_set.cases[case_id], gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    (staging / "outputs" / f"{case_id}.json").write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    completed += 1
                    print(
                        f"[{completed}/100] {case_id}: {output['assessment']['primary_issue']}",
                        flush=True,
                    )

            async with asyncio.TaskGroup() as tasks:
                for case_id in case_set.case_ids:
                    tasks.create_task(run_one(case_id))
        validate_artifacts(staging, case_set, contracts)
        existing = list(output_root.glob("*.json")) + ([trace_path] if trace_path.exists() else [])
        if existing:
            backup = root / "dist" / "previous-runs" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            for original in existing:
                destination = backup / original.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, destination)
        output_root.mkdir(parents=True, exist_ok=True)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        for generated in (staging / "outputs").glob("*.json"):
            target = output_root / generated.name
            temporary = target.with_suffix(".json.tmp")
            shutil.copy2(generated, temporary)
            temporary.replace(target)
        temporary_trace = trace_path.with_suffix(".jsonl.tmp")
        shutil.copy2(staging / "traces" / "trace.jsonl", temporary_trace)
        temporary_trace.replace(trace_path)


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
    except ExceptionGroup as group:
        # MCP transports use task groups; do not print nested request context/secrets.
        pending: list[BaseException] = list(group.exceptions)
        leaves = []
        while pending:
            failure = pending.pop()
            if isinstance(failure, BaseExceptionGroup):
                pending.extend(failure.exceptions)
            else:
                leaves.append(failure)
        controlled = next((e for e in leaves if type(e) in {RuntimeError, ValueError}), None)
        detail = (
            str(controlled) if controlled else ", ".join(sorted({type(e).__name__ for e in leaves}))
        )
        print(f"ERROR: MCP run failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
