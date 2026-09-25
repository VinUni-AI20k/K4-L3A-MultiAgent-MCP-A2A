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
from .model_client import OpenRouterClient
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


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


async def _run(
    root: Path, *, resume: bool = False, rerun_case_ids: tuple[str, ...] = ()
) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    rerun = set(rerun_case_ids)
    unknown = sorted(rerun - set(case_set.case_ids))
    if unknown:
        raise ValueError(f"unknown --rerun-case IDs: {unknown}")
    if rerun and not resume:
        raise ValueError("--rerun-case requires --resume")
    completed: set[str] = set()
    if resume:
        completed = _prepare_resume(
            output_root, trace_path, case_set.case_ids, contracts, rerun=rerun
        )
    else:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)
    model = OpenRouterClient.from_env()

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
    for index, case_id in enumerate(case_set.case_ids, 1):
        if case_id in completed:
            print(f"[{index}/{len(case_set.case_ids)}] {case_id} (kept)")
            continue
        case = case_set.cases[case_id]
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await _solve_with_retry(
            case,
            settings=settings,
            contracts=contracts,
            trace=trace,
            model=model,
        )
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
        print(f"[{index}/{len(case_set.case_ids)}] {case_id}")


async def _solve_with_retry(
    case: dict[str, object],
    *,
    settings: Settings,
    contracts: Contracts,
    trace: TraceWriter,
    model: OpenRouterClient,
) -> dict[str, object]:
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                return await solve_case(case, gateway, trace, model=model)
        except ValueError:
            raise
        except Exception as exc:
            if attempt == attempts:
                raise RuntimeError(
                    f"case {case.get('case_id')} failed after {attempts} attempts"
                ) from exc
            print(
                f"WARN: retrying {case.get('case_id')} after transient MCP error "
                f"({attempt}/{attempts})",
                file=sys.stderr,
            )
            await asyncio.sleep(attempt)
    raise AssertionError("unreachable")


def _prepare_resume(
    output_root: Path,
    trace_path: Path,
    case_ids: tuple[str, ...],
    contracts: Contracts,
    *,
    rerun: set[str] | None = None,
) -> set[str]:
    expected = set(case_ids)
    forced = rerun or set()
    completed: set[str] = set()
    for path in output_root.glob("*.json"):
        if path.stem not in expected:
            continue
        if path.stem in forced:
            path.unlink()
            continue
        try:
            output = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        contracts.validate_output(output, str(path))
        if output.get("case_id") == path.stem:
            completed.add(path.stem)

    if trace_path.exists():
        kept_lines: list[str] = []
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("case_id") in completed:
                kept_lines.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        trace_path.write_text(
            "\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8"
        )
    return completed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    mcp_tools = commands.add_parser(
        "mcp-tools", help="authenticate and list discovered MCP tools"
    )
    mcp_tools.add_argument("--json", action="store_true", help="include tool descriptions")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true", help="keep valid completed cases and continue"
    )
    run.add_argument(
        "--rerun-case",
        action="append",
        default=[],
        help="with --resume, regenerate one case ID (repeatable)",
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
            asyncio.run(
                _run(root, resume=args.resume, rerun_case_ids=tuple(args.rerun_case))
            )
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
