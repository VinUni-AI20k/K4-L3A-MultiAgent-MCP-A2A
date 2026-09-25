from __future__ import annotations

import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from .cases import CaseSet
from .contracts import Contracts

SECRET_PATTERN = re.compile(r"sk-team-[A-Za-z0-9_-]{8,}")
MAX_FILE_BYTES = 1024 * 1024
MAX_SUBMISSION_BYTES = 12 * 1024 * 1024
REQUIRED_LIFECYCLE = (
    "case_received",
    "task_assigned",
    "tool_result_consumed",
    "handoff",
    "policy_decided",
    "verification_completed",
    "case_finalized",
)


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def build_manifest(case_set: CaseSet) -> dict[str, Any]:
    return {
        "schema_version": "day09-submission-manifest-v2",
        "competition_id": "day09-multiagent-mcp-a2a",
        "variant_id": VARIANT_ID,
        "case_set_version": case_set.version,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "trace_schema_version": "day09-trace-event-v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "client": {"name": "day09-student-starter", "version": "0.1.0"},
    }


def validate_artifacts(
    root: Path, case_set: CaseSet, contracts: Contracts
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    outputs_root = root / "outputs"
    actual = {path.stem: path for path in outputs_root.glob("*.json") if path.is_file()}
    expected = set(case_set.case_ids)
    if set(actual) != expected:
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        raise ValueError(f"outputs do not match case-set; missing={missing}, extra={extra}")

    outputs: dict[str, dict[str, Any]] = {}
    for case_id in case_set.case_ids:
        output = _json_object(actual[case_id])
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"outputs/{case_id}.json has a mismatched case_id")
        if not output.get("evidence_refs"):
            raise ValueError(f"outputs/{case_id}.json has no auditable evidence_refs")
        empty_claims = [
            item.get("claim_id", "<unknown>")
            for item in output.get("claim_assessments", [])
            if not item.get("evidence_refs")
        ]
        if empty_claims:
            raise ValueError(
                f"outputs/{case_id}.json has claims without evidence_refs: {empty_claims}"
            )
        outputs[case_id] = output

    trace_path = root / "traces" / "trace.jsonl"
    try:
        trace_lines = trace_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("traces/trace.jsonl is missing or not UTF-8") from exc
    normalized_lines: list[str] = []
    events_by_case: dict[str, list[dict[str, Any]]] = {
        case_id: [] for case_id in case_set.case_ids
    }
    seen_events: set[str] = set()
    for number, line in enumerate(trace_lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"traces/trace.jsonl:{number}: invalid JSON") from exc
        contracts.validate_trace(event, f"traces/trace.jsonl:{number}")
        if event["case_id"] not in expected:
            raise ValueError(f"traces/trace.jsonl:{number}: case is outside this case-set")
        if event["event_id"] in seen_events:
            raise ValueError(f"traces/trace.jsonl:{number}: duplicate event_id")
        seen_events.add(event["event_id"])
        events_by_case[event["case_id"]].append(event)
        normalized_lines.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    for case_id in case_set.case_ids:
        case_events = events_by_case[case_id]
        event_types = [event["event_type"] for event in case_events]
        missing = [event_type for event_type in REQUIRED_LIFECYCLE if event_type not in event_types]
        if missing:
            raise ValueError(f"traces/trace.jsonl:{case_id}: missing lifecycle events {missing}")
        if event_types[0] != "case_received" or event_types[-1] != "case_finalized":
            raise ValueError(f"traces/trace.jsonl:{case_id}: invalid lifecycle boundaries")
        positions = [event_types.index(event_type) for event_type in REQUIRED_LIFECYCLE]
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            raise ValueError(f"traces/trace.jsonl:{case_id}: lifecycle events are out of order")
        if event_types.count("case_received") != 1 or event_types.count("case_finalized") != 1:
            raise ValueError(f"traces/trace.jsonl:{case_id}: duplicate lifecycle boundary")

        output_refs = set(outputs[case_id]["evidence_refs"])
        consumed_refs = {
            evidence_ref
            for event in case_events
            if event["event_type"] == "tool_result_consumed"
            for evidence_ref in event.get("evidence_refs", [])
        }
        if not output_refs.issubset(consumed_refs):
            raise ValueError(
                f"traces/trace.jsonl:{case_id}: output evidence lacks tool consumption"
            )
        for claim in outputs[case_id].get("claim_assessments", []):
            if not set(claim["evidence_refs"]).issubset(output_refs):
                raise ValueError(
                    f"traces/trace.jsonl:{case_id}: claim evidence is outside output evidence"
                )

    serialized = [json.dumps(value, ensure_ascii=False) for value in outputs.values()]
    if SECRET_PATTERN.search("\n".join([*serialized, *normalized_lines])):
        raise ValueError("a Team API Key appears in output or trace")
    return outputs, normalized_lines


def package_submission(root: Path, destination: Path) -> Path:
    from .cases import load_case_set

    root = root.resolve()
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    outputs, trace_lines = validate_artifacts(root, case_set, contracts)
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)

    payloads = {
        "manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
        "trace.jsonl": ("\n".join(trace_lines) + ("\n" if trace_lines else "")).encode(),
        **{
            f"outputs/{case_id}.json": json.dumps(
                outputs[case_id], ensure_ascii=False, separators=(",", ":")
            ).encode()
            for case_id in case_set.case_ids
        },
    }
    oversized = [name for name, payload in payloads.items() if len(payload) > MAX_FILE_BYTES]
    if oversized:
        raise ValueError(f"submission files exceed 1 MB: {oversized}")
    if sum(map(len, payloads.values())) > MAX_SUBMISSION_BYTES:
        raise ValueError("submission exceeds the 12 MB uncompressed limit")

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    return destination
