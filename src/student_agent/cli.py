from __future__ import annotations

import argparse
import asyncio
import json
import sys
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
        response = await gateway._session.list_tools()
        for tool in sorted(response.tools, key=lambda t: t.name):
            print(f"[{tool.name}]")
            if tool.description:
                print(f"  Description: {tool.description}")
            if tool.inputSchema:
                props = tool.inputSchema.get("properties", {})
                req = tool.inputSchema.get("required", [])
                args_str = ", ".join(f"{k}{'*' if k in req else ''}" for k in props)
                print(f"  Args: {args_str}")

        print("\n--- Diagnostic sample call (get_order) ---")
        try:
            sample = await gateway.call(
                "get_order",
                case_id="L3A_CASE_001",
                order_id="e2a03ccf5ea816036608b2d8c3ab8e60",
            )
            print("SUCCESS! Keys:", list(sample.keys()))
            print("evidence_ref:", sample.get("evidence_ref"))
            print("data:", sample.get("data"))
        except Exception as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}")


async def _run(root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if resume:
        done = {path.stem for path in output_root.glob("*.json")}
        # Drop trace events of cases that crashed mid-flight so they are not duplicated.
        if trace_path.exists():
            kept = [
                line
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line).get("case_id") in done
            ]
            trace_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        print(f"Resuming: {len(done)} cases already done; keeping their trace events.")
    else:
        done = set()
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    total_cases = len(case_set.case_ids)
    print(f"Connecting to MCP Gateway at {settings.mcp_endpoint} ...")

    gateway_cm = None
    gateway = None

    async def get_or_reconnect_gateway():
        nonlocal gateway_cm, gateway
        if gateway_cm is not None:
            try:
                await gateway_cm.__aexit__(None, None, None)
            except Exception:
                pass
        attempts = 6
        for att in range(1, attempts + 1):
            try:
                gateway_cm = connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts)
                gateway = await gateway_cm.__aenter__()
                return gateway
            except Exception as e:
                gateway_cm = None
                if att == attempts:
                    raise
                delay = min(2 * 2 ** (att - 1), 30)
                print(f"Reconnect attempt {att} failed: {e}. Retrying in {delay}s...")
                await asyncio.sleep(delay)

    try:
        gateway = await get_or_reconnect_gateway()
        discovered_tools = await gateway.list_tools()
        print(f"Connected to MCP Gateway successfully! Discovered {len(discovered_tools)} tools.\n")
        print(f"Starting Multi-Agent processing for {total_cases} cases...")

        for idx, case_id in enumerate(case_set.case_ids, start=1):
            case = case_set.cases[case_id]
            topic = (
                case.get("customer_request", {})
                .get("claims", [{}])[0]
                .get("topic", "investigation")
            )
            print(f"[{idx:03d}/{total_cases}] Case {case_id} ({topic})...", end=" ", flush=True)

            if case_id in done:
                print("SKIP (already done)", flush=True)
                continue

            for case_attempt in range(1, 5):
                try:
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
                    break
                except (Exception, asyncio.CancelledError) as exc:
                    if case_attempt == 4:
                        print(f"\nFATAL: Failed case {case_id}: {exc}", file=sys.stderr)
                        raise
                    print(f"[reconnecting due to {type(exc).__name__}]", end=" ", flush=True)
                    gateway = await get_or_reconnect_gateway()

            status = output.get("assessment", {}).get("case_status", "")
            refund = output.get("financial_resolution", {}).get("recommended_refund_brl", 0.0)
            refs_count = len(output.get("evidence_refs", []))
            print(f"OK [{status}, refund={refund} BRL, ev_refs={refs_count}]", flush=True)

        print(f"\nAll {total_cases} cases processed and validated successfully!")
    finally:
        if gateway_cm is not None:
            try:
                await gateway_cm.__aexit__(None, None, None)
            except Exception:
                pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run_cmd = commands.add_parser("run", help="run the implemented workflow for all cases")
    run_cmd.add_argument(
        "--resume",
        action="store_true",
        help="keep existing outputs/trace and only process cases that are still missing",
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
