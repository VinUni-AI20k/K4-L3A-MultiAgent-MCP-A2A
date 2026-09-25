from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import ContractError, Contracts

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "contracts" / "schemas"
EXPECTED_SCHEMAS = {
    "l3a-output-v2.schema.json",
    "l3b-output-v2.schema.json",
    "mcp-evidence-response-v1.schema.json",
    "submission-manifest-v2.schema.json",
    "trace-event-v1.schema.json",
}


def valid_values() -> dict[str, dict[str, Any]]:
    return {
        "trace-event-v1.schema.json": {
            "schema_version": "day09-trace-event-v1",
            "event_id": "evt_abcdefghijklmnop",
            "case_id": "CASE_001",
            "event_type": "case_received",
            "occurred_at": "2026-01-01T00:00:00Z",
            "actor": "coordinator",
        },
        "submission-manifest-v2.schema.json": {
            "schema_version": "day09-submission-manifest-v2",
            "competition_id": "day09-multiagent-mcp-a2a",
            "variant_id": "l3a",
            "case_set_version": "test-v1",
            "output_schema_version": "day09-l3a-output-v2",
            "trace_schema_version": "day09-trace-event-v1",
            "generated_at": "2026-01-01T00:00:00Z",
        },
        "mcp-evidence-response-v1.schema.json": {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_abcdefghijklmnopqrstuvwxyz",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {},
        },
    }


def test_public_schema_inventory_and_closed_roots_are_locked() -> None:
    paths = {path.name for path in SCHEMA_ROOT.glob("*.schema.json")}
    assert paths == EXPECTED_SCHEMAS
    for name in paths:
        schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize("schema_name", sorted(valid_values()))
def test_non_output_public_contracts_reject_unknown_top_level_fields(
    schema_name: str,
) -> None:
    contracts = Contracts(SCHEMA_ROOT)
    value = valid_values()[schema_name]
    contracts.validate(schema_name, value, "valid contract")
    with pytest.raises(ContractError, match="Additional properties"):
        contracts.validate(schema_name, {**value, "unexpected": True}, "invalid contract")


def test_scoring_policy_contract_is_available_to_policy_agent() -> None:
    policy = Contracts(SCHEMA_ROOT).load_scoring_policy()
    assert policy["policy_version"] == "day09-scoring-v2"
    assert "l3a" in policy["variant_weights"]
    assert "case_finalized" in policy["workflow_required_events"]
    assert "cross_scope_evidence_ref" in policy["hard_gates"]
