from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import VARIANT_ID

CASE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")


@dataclass(frozen=True)
class CaseSet:
    version: str
    variant_id: str
    case_ids: tuple[str, ...]
    cases: dict[str, dict[str, Any]]


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def load_case_set(root: Path, expected_count: int | None = None) -> CaseSet:
    root = root.resolve()
    manifest = _object(root / "case-set.json")
    if set(manifest) != {"case_set_version", "variant_id", "case_ids"}:
        raise ValueError("case-set.json has unexpected or missing fields")
    if manifest["variant_id"] != VARIANT_ID:
        raise ValueError(f"expected variant {VARIANT_ID}, got {manifest['variant_id']!r}")
    raw_ids = manifest["case_ids"]
    if not isinstance(raw_ids, list) or not all(isinstance(item, str) for item in raw_ids):
        raise ValueError("case_ids must be an array of strings")
    if not raw_ids or len(set(raw_ids)) != len(raw_ids):
        raise ValueError("case-set must contain a non-empty list of unique case IDs")
    if expected_count is not None and len(raw_ids) != expected_count:
        raise ValueError(f"case-set must contain exactly {expected_count} unique case IDs")
    if any(not CASE_ID_PATTERN.fullmatch(case_id) for case_id in raw_ids):
        raise ValueError("case-set contains an invalid case ID")
    version = manifest["case_set_version"]
    if not isinstance(version, str) or not version:
        raise ValueError("case_set_version must be a non-empty string")

    input_root = root / "inputs"
    actual_files = {path.stem: path for path in input_root.glob("*.json") if path.is_file()}
    if set(actual_files) != set(raw_ids):
        missing = sorted(set(raw_ids) - set(actual_files))
        extra = sorted(set(actual_files) - set(raw_ids))
        raise ValueError(f"inputs do not match case-set; missing={missing}, extra={extra}")
    cases = {case_id: _object(actual_files[case_id]) for case_id in raw_ids}
    for case_id, case in cases.items():
        if case.get("case_id") != case_id:
            raise ValueError(f"inputs/{case_id}.json has a mismatched case_id")
    return CaseSet(version, VARIANT_ID, tuple(raw_ids), cases)
