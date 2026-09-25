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
    completed = _prepare_resume(case_set.case_ids, output_root, trace_path, contracts)

    # Fail fast before the batch, but do not keep one fragile MCP session for all cases.
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")

    trace = TraceWriter(trace_path, contracts)
    total = len(case_set.case_ids)
    for index, case_id in enumerate(case_set.case_ids, start=1):
        if case_id in completed:
            print(f"[{index}/{total}] {case_id}: checkpoint OK, skipped", flush=True)
            continue
        for attempt, delay in enumerate((0.0, 2.0, 5.0, 10.0, 20.0), start=1):
            if delay:
                await asyncio.sleep(delay)
            if attempt > 1:
                _remove_case_trace(trace_path, case_id)
            print(
                f"[{index}/{total}] {case_id}: running (attempt {attempt}/5)",
                flush=True,
            )
            try:
                await _run_case(
                    case_set.cases[case_id],
                    output_root,
                    trace,
                    contracts,
                    settings.mcp_endpoint,
                    settings.team_api_key,
                )
            except Exception as exc:
                if attempt == 5:
                    raise RuntimeError(
                        f"{case_id} failed after {attempt} attempts: "
                        f"{_exception_summary(exc)}"
                    ) from exc
                print(
                    f"[{index}/{total}] {case_id}: transient failure; reconnecting: "
                    f"{_exception_summary(exc)}",
                    flush=True,
                )
            else:
                print(f"[{index}/{total}] {case_id}: completed", flush=True)
                break


async def _run_case(
    case: dict[str, object],
    output_root: Path,
    trace: TraceWriter,
    contracts: Contracts,
    mcp_endpoint: str,
    team_api_key: str,
) -> None:
    case_id = str(case["case_id"])
    async with connect_gateway(
        mcp_endpoint,
        team_api_key,
        contracts,
        preflight=False,
    ) as gateway:
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


def _remove_case_trace(trace_path: Path, case_id: str) -> None:
    if not trace_path.exists():
        return
    kept: list[str] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("case_id") != case_id:
            kept.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    temporary = trace_path.with_suffix(".jsonl.tmp")
    temporary.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    temporary.replace(trace_path)


def _exception_summary(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _exception_summary(exc.exceptions[0])
    message = str(exc).strip() or "no details"
    return f"{exc.__class__.__name__}: {message}"


def _prepare_resume(
    case_ids: tuple[str, ...],
    output_root: Path,
    trace_path: Path,
    contracts: Contracts,
) -> set[str]:
    """Keep only complete, contract-valid checkpoints and discard partial trace runs."""
    expected = set(case_ids)
    trace_events: list[dict[str, object]] = []
    finalized: set[str] = set()
    if trace_path.exists():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                contracts.validate_trace(event, "resume trace")
            except (json.JSONDecodeError, ValueError):
                continue
            if event["case_id"] in expected:
                trace_events.append(event)
                if event["event_type"] == "case_finalized":
                    finalized.add(event["case_id"])

    completed: set[str] = set()
    for case_id in finalized:
        target = output_root / f"{case_id}.json"
        try:
            output = json.loads(target.read_text(encoding="utf-8"))
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                continue
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        completed.add(case_id)

    kept_events = [event for event in trace_events if event["case_id"] in completed]
    temporary = trace_path.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in kept_events
        ),
        encoding="utf-8",
    )
    temporary.replace(trace_path)
    return completed


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
