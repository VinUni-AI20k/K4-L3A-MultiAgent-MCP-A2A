from __future__ import annotations

import argparse
import asyncio
import json
import shutil
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


def _async(coroutine: object) -> None:
    """Run an async CLI action and unwrap AnyIO task-group errors for users."""
    try:
        asyncio.run(coroutine)  # type: ignore[arg-type]
    except BaseException as exc:
        if exc.__class__.__name__ not in {"ExceptionGroup", "BaseExceptionGroup"}:
            raise
        leaves: list[BaseException] = []
        pending = [exc]
        while pending:
            current = pending.pop()
            children = getattr(current, "exceptions", None)
            if children:
                pending.extend(children)
            else:
                leaves.append(current)
        leaf = next((item for item in leaves if str(item).strip()), leaves[0])
        message = str(leaf).strip() or leaf.__class__.__name__
        raise RuntimeError(message) from None


async def _show_tools(root: Path, verbose: bool = False) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        if verbose:
            print(json.dumps(await gateway.describe_tools(), ensure_ascii=False, indent=2))
        else:
            for tool in await gateway.list_tools():
                print(tool)


async def _run(root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    staging_root = root / ".run-staging"
    staging_outputs = staging_root / "outputs"
    backup_outputs = staging_root / "backup-outputs"
    staging_trace = staging_root / "trace.jsonl"
    staging_outputs.mkdir(parents=True, exist_ok=True)
    backup_outputs.mkdir(parents=True, exist_ok=True)
    if resume:
        completed = {path.stem for path in staging_outputs.glob("*.json")}
        if not completed:
            raise RuntimeError("no staged outputs found; run `day09 run` without --resume")
        if staging_trace.exists():
            valid_lines = []
            for line in staging_trace.read_text(encoding="utf-8").splitlines():
                if line.strip() and json.loads(line)["case_id"] in completed:
                    valid_lines.append(line)
            staging_trace.write_text(
                "\n".join(valid_lines) + ("\n" if valid_lines else ""), encoding="utf-8"
            )
        for stale in output_root.glob("*.json"):
            stale.unlink()
        for staged in staging_outputs.glob("*.json"):
            shutil.copy2(staged, output_root / staged.name)
        print(f"Resuming after {len(completed)}/{len(case_set.case_ids)} completed cases")
    else:
        for stale in staging_outputs.glob("*.json"):
            stale.unlink()
        for stale in backup_outputs.glob("*.json"):
            stale.unlink()
        for current in output_root.glob("*.json"):
            shutil.copy2(current, backup_outputs / current.name)
        for stale in output_root.glob("*.json"):
            stale.unlink()
        staging_trace.unlink(missing_ok=True)
        completed = set()
    trace = TraceWriter(staging_trace, contracts)

    try:
        async with connect_gateway(
            settings.mcp_endpoint, settings.team_api_key, contracts
        ) as gateway:
            discovered_tools = await gateway.list_tools()
            if not discovered_tools:
                raise RuntimeError("MCP Gateway returned no tools")
            total = len(case_set.case_ids)
            for index, case_id in enumerate(case_set.case_ids, 1):
                if case_id in completed:
                    continue
                case = case_set.cases[case_id]
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                output = await solve_case(case, gateway, trace)
                contracts.validate_output(output, f"outputs/{case_id}.json")
                if output.get("case_id") != case_id:
                    raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                target = staging_outputs / f"{case_id}.json"
                temporary = target.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(target)
                trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")

                # Make completed cases visible immediately while retaining a rollback copy.
                shutil.copy2(target, output_root / target.name)
                print(f"[{index}/{total}] wrote outputs/{target.name}", flush=True)
    except BaseException:
        print(
            "Run interrupted; completed files were kept. Retry with `day09 run --resume`.",
            file=sys.stderr,
        )
        raise

    # Publish only after every case succeeded, preserving the last valid run on failure.
    for stale in output_root.glob("*.json"):
        stale.unlink()
    for staged in staging_outputs.glob("*.json"):
        staged.replace(output_root / staged.name)
    staging_trace.replace(trace_path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    mcp_tools = commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    mcp_tools.add_argument(
        "--verbose", action="store_true", help="include descriptions and input schemas"
    )
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true", help="continue from completed .run-staging outputs"
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
            _async(_show_tools(root, args.verbose))
        elif args.command == "run":
            _async(_run(root, args.resume))
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
