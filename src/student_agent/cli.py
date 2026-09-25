from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .llm import LLMClient
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import AgentModels, solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
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


def _resume_cases(
    root: Path, case_ids: list[str], contracts: Contracts
) -> set[str]:
    trace_path = root / "traces" / "trace.jsonl"
    events: list[dict[str, object]] = []
    if trace_path.exists():
        for number, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"traces/trace.jsonl:{number}: invalid JSON") from exc
            contracts.validate_trace(event, f"traces/trace.jsonl:{number}")
            events.append(event)
    finalized = {
        str(event["case_id"])
        for event in events
        if event.get("event_type") == "case_finalized"
    }
    completed: set[str] = set()
    for case_id in case_ids:
        target = root / "outputs" / f"{case_id}.json"
        if case_id not in finalized or not target.is_file():
            continue
        try:
            output = json.loads(target.read_text(encoding="utf-8"))
            contracts.validate_output(output, f"outputs/{case_id}.json")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if output.get("case_id") == case_id:
            completed.add(case_id)
    retained = [event for event in events if event.get("case_id") in completed]
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                for event in retained),
        encoding="utf-8",
    )
    return completed


async def _run(root: Path, *, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if resume:
        completed = _resume_cases(root, case_set.case_ids, contracts)
    else:
        # A fresh run establishes the scope used by the MCP audit and the scorer.
        # Resume must not rotate this scope because completed evidence refs belong
        # to the already-active run.
        await _start_competition_run(settings, case_set)
        completed = set()
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    llm = LLMClient.from_settings(settings)
    models = AgentModels(
        coordinator=settings.coordinator_model,
        order_payment=settings.order_payment_model,
        shipment_seller=settings.shipment_seller_model,
        policy_resolution=settings.policy_resolution_model,
        verifier=settings.verifier_model,
    )

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        for case_id in case_set.case_ids:
            if case_id in completed:
                print(f"SKIP: {case_id} (completed)", flush=True)
                continue
            case = case_set.cases[case_id]
            temporary_trace = trace_path.parent / f".{case_id}.jsonl.tmp"
            temporary_trace.unlink(missing_ok=True)
            trace = TraceWriter(temporary_trace, contracts)
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(
                case, gateway, trace, llm=llm, models=models,
                max_parallel_specialists=settings.llm_max_parallel_specialists,
            )
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
            temporary.replace(target)
            with trace_path.open("a", encoding="utf-8") as destination:
                destination.write(temporary_trace.read_text(encoding="utf-8"))
            temporary_trace.unlink()
            print(f"OK: {case_id}", flush=True)


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
            asyncio.run(_show_tools(root))
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
