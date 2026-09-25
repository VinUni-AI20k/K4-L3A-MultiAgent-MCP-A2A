from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from student_agent import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from student_agent.cases import CaseSet, load_case_set
from student_agent.contracts import Contracts
from student_agent.submission import build_manifest
from student_agent.trace import TraceWriter
from student_agent.workflow import _CaseWorkflow, solve_case


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_load_case_set_rejects_wrong_variant(tmp_path: Path) -> None:
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": "l3b", "case_ids": ["CASE_001"]},
    )
    write_json(tmp_path / "inputs" / "CASE_001.json", {"case_id": "CASE_001"})
    with pytest.raises(ValueError, match="expected variant"):
        load_case_set(tmp_path, expected_count=1)


def test_load_case_set_accepts_exact_input_inventory(tmp_path: Path) -> None:
    case_ids = ["CASE_001", "CASE_002"]
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": VARIANT_ID, "case_ids": case_ids},
    )
    for case_id in case_ids:
        write_json(tmp_path / "inputs" / f"{case_id}.json", {"case_id": case_id})
    loaded = load_case_set(tmp_path, expected_count=2)
    assert loaded.case_ids == tuple(case_ids)


def test_generated_manifest_matches_public_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case_set = CaseSet("test-v1", VARIANT_ID, ("CASE_001",), {})
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)
    assert manifest["output_schema_version"] == OUTPUT_SCHEMA_VERSION


def workflow_fixture(issue: str) -> tuple[dict, dict]:
    """Synthetic unit-test evidence only; never used by the production gateway."""
    case = {
        "case_id": "TEST_CASE_001",
        "opened_at": "2018-02-01T00:00:00Z",
        "policy_version": "TEST_POLICY",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-1", "topic": issue},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
    }
    order = {
        "order_id": "order-1",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-01-01T00:00:00Z",
        "order_estimated_delivery_date": "2018-01-15T00:00:00Z",
        "order_delivered_carrier_date": "2018-01-02T00:00:00Z",
        "order_delivered_customer_date": "2018-01-10T00:00:00Z",
    }
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        order["order_status"] = issue.split("_")[0]
        order["order_delivered_customer_date"] = None
    if issue.startswith("late_delivery"):
        order["order_delivered_customer_date"] = "2018-01-20T00:00:00Z"
    if issue == "late_delivery_seller":
        order["order_delivered_carrier_date"] = "2018-01-06T00:00:00Z"
    amounts = (
        [45, 45]
        if issue == "valid_split_payment"
        else [90, 90]
        if (issue == "duplicate_charge")
        else [90]
    )
    events = [
        dict(
            order_id="order-1",
            event_at=f"2018-01-01T0{i + 1}:00:00Z",
            event_type="captured",
            amount_brl=amount,
            status="confirmed",
        )
        for i, amount in enumerate(amounts)
    ]
    if issue == "payment_mismatch":
        events.append(
            dict(
                order_id="order-1",
                event_at="2018-01-02T00:00:00Z",
                event_type="reconciliation_mismatch",
                amount_brl=20,
                status="open",
            )
        )
    status = (
        "no_action"
        if issue in {"valid_split_payment", "unsupported_claim"}
        else ("needs_investigation" if issue == "refund_pending" else "action_required")
    )
    party = (
        "seller"
        if issue in {"late_delivery_seller", "unavailable_order_paid"}
        else "logistics_provider"
        if issue == "late_delivery_logistics"
        else "platform"
        if issue == "canceled_order_paid"
        else "customer"
        if status == "no_action"
        else "payment_provider"
    )
    amount = (
        0
        if status != "action_required"
        else 10
        if issue.startswith("late_")
        else (20 if issue == "payment_mismatch" else 90)
    )
    data = {
        "get_order": order,
        "get_order_items": [
            dict(
                order_id="order-1",
                order_item_id="item-1",
                seller_id="seller-1",
                shipping_limit_date="2018-01-04T00:00:00Z",
                price=80,
                freight_value=10,
            )
        ],
        "get_payment_timeline": dict(order_id="order-1", payments=[], events=events),
        "get_refund_timeline": dict(
            order_id="order-1",
            events=[
                dict(
                    order_id="order-1",
                    event_at="2018-01-25T00:00:00Z",
                    event_type="refund_requested",
                    amount_brl=90,
                    status="failed" if issue == "refund_failed" else "pending",
                )
            ]
            if issue.startswith("refund_")
            else [],
        ),
        "get_shipment_summary": dict(
            order_id="order-1",
            delivered_carrier_at=order["order_delivered_carrier_date"],
            delivered_customer_at=order["order_delivered_customer_date"],
            estimated_delivery_at=order["order_estimated_delivery_date"],
            events=[],
        ),
        "get_policy": dict(
            policy_version="TEST_POLICY",
            currency="BRL",
            rules={
                issue: dict(
                    case_status=status,
                    refund_brl=amount,
                    recommended_action=(
                        "document_no_action" if status == "no_action" else "resolve_dispute"
                    ),
                    responsible_parties=[
                        dict(party_type=party, party_id="seller-1" if party == "seller" else None)
                    ],
                )
            },
        ),
    }
    return case, data


class FakeGateway:
    def __init__(self, data: dict, case_id: str):
        self.data, self.case_id = data, case_id
        self.calls: list[str] = []
        self.failures: dict[str, list[Exception]] = {}

    async def list_tools(self) -> list[str]:
        return list(self.data)

    async def call(self, tool: str, *, case_id: str, **arguments: str) -> dict:
        assert case_id == self.case_id
        assert arguments == (
            {"policy_version": "TEST_POLICY"} if tool == "get_policy" else {"order_id": "order-1"}
        )
        self.calls.append(tool)
        if self.failures.get(tool):
            raise self.failures[tool].pop(0)
        domain = {
            "get_order": "order",
            "get_order_items": "item",
            "get_payment_timeline": "payment",
            "get_refund_timeline": "refund",
            "get_shipment_summary": "shipment",
            "get_policy": "policy",
        }[tool]
        return dict(
            schema_version="day09-mcp-evidence-v1",
            domain=domain,
            evidence_ref=f"ev_{len(self.calls):024d}",
            result_hash="sha256:" + "0" * 64,
            data=copy.deepcopy(self.data[tool]),
        )


def run_workflow(tmp_path: Path, case: dict, data: dict, gateway=None):
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    gateway = gateway or FakeGateway(data, case["case_id"])
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    return output, [json.loads(line) for line in trace.path.read_text().splitlines()]


@pytest.mark.parametrize(
    "issue",
    [
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
    ],
)
def test_workflow_business_branches_and_lifecycle(tmp_path: Path, issue: str) -> None:
    case, data = workflow_fixture(issue)
    output, events = run_workflow(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == issue
    assert (
        output["financial_resolution"]["recommended_refund_brl"]
        == data["get_policy"]["rules"][issue]["refund_brl"]
    )
    types = [event["event_type"] for event in events]
    assert types[0] == "case_received" and types[-1] == "case_finalized"
    assert {
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
    } <= set(types)
    assert types.index("policy_decided") < types.index("verification_completed")
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    assert output["assessment"]["confidence"] < 1


def test_customer_claim_does_not_override_evidence(tmp_path: Path) -> None:
    case, data = workflow_fixture("valid_split_payment")
    case["customer_request"]["claims"][0]["topic"] = "duplicate_charge"
    data["get_policy"]["rules"]["duplicate_charge"] = dict(
        case_status="action_required",
        refund_brl=45,
        recommended_action="refund_duplicate_charge",
        responsible_parties=[dict(party_type="payment_provider", party_id=None)],
    )
    output, _ = run_workflow(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


@pytest.mark.parametrize(
    "tool",
    ["get_order", "get_order_items", "get_policy", "get_payment_timeline", "get_refund_timeline"],
)
def test_missing_evidence_does_not_fabricate_decision(tmp_path: Path, tool: str) -> None:
    case, data = workflow_fixture("refund_failed")
    del data[tool]
    output, _ = run_workflow(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_cross_scope_and_conflicting_data_are_rejected(tmp_path: Path) -> None:
    case, data = workflow_fixture("valid_split_payment")
    data["get_payment_timeline"]["events"][0]["order_id"] = "other-order"
    output, _ = run_workflow(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


def test_timeline_duplicates_and_policy_seller_are_reconciled(tmp_path: Path) -> None:
    case, data = workflow_fixture("late_delivery_seller")
    row = dict(data["get_order_items"][0], shipping_limit_date="2017-12-01T00:00:00Z")
    data["get_order_items"].append(row)
    event = dict(data["get_payment_timeline"]["events"][0], event_at="2018-03-01T00:00:00Z")
    data["get_payment_timeline"]["events"].append(event)
    data["get_policy"]["rules"]["late_delivery_seller"]["responsible_parties"][0]["party_id"] = (
        "other-seller"
    )
    output, _ = run_workflow(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert len(output["data_conflicts"]) == 3
    assert output["assessment"]["confidence"] < 0.9
    assert output["root_cause_analysis"]["responsible_parties"][0]["party_id"] == "seller-1"


def test_retries_are_bounded_and_access_errors_are_not_retried(tmp_path: Path) -> None:
    case, data = workflow_fixture("unsupported_claim")
    gateway = FakeGateway(data, case["case_id"])
    gateway.failures["get_order"] = [TimeoutError(), TimeoutError(), TimeoutError()]
    gateway.failures["get_order_items"] = [RuntimeError("403 Forbidden")]
    output, events = run_workflow(tmp_path, case, data, gateway)
    assert gateway.calls.count("get_order") == 3
    assert gateway.calls.count("get_order_items") == 1
    assert sum(event.get("decision_code") == "RETRY" for event in events) == 2
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


def test_verifier_rejects_tampered_refund(tmp_path: Path) -> None:
    case, data = workflow_fixture("canceled_order_paid")
    output, _ = run_workflow(tmp_path, case, data)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    flow = _CaseWorkflow(
        case, FakeGateway(data, case["case_id"]), TraceWriter(tmp_path / "verify.jsonl", contracts)
    )
    flow.evidence = {ref: {"evidence_ref": ref} for ref in output["evidence_refs"]}
    output["financial_resolution"]["refund_lines"][0]["amount_brl"] = 1
    with pytest.raises(ValueError, match="refund total"):
        flow.verify(output, data["get_policy"]["rules"]["canceled_order_paid"])


def test_gateway_supports_mcp_v2_result_fields() -> None:
    from student_agent.mcp_gateway import EvidenceGateway

    class Session:
        async def call_tool(self, *args, **kwargs):
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text="denied")])

    gateway = EvidenceGateway(Session(), None)
    with pytest.raises(RuntimeError, match="denied"):
        asyncio.run(gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1"))


@pytest.mark.parametrize("problem", ["seller", "timestamp", "shipment", "refund_amount"])
def test_invalid_or_conflicting_evidence_requires_investigation(tmp_path: Path, problem: str):
    case, data = workflow_fixture("valid_split_payment")
    if problem == "seller":
        del data["get_order_items"][0]["seller_id"]
    elif problem == "timestamp":
        data["get_payment_timeline"]["events"][0]["event_at"] = "invalid"
    elif problem == "shipment":
        data["get_shipment_summary"]["estimated_delivery_at"] = "2018-02-15T00:00:00Z"
    else:
        data["get_policy"]["rules"]["valid_split_payment"]["refund_brl"] = 200
    output, _ = run_workflow(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


@pytest.mark.parametrize("problem", ["claim", "entity", "ref", "responsibility"])
def test_verifier_rejects_unlinked_output(tmp_path: Path, problem: str):
    case, data = workflow_fixture("late_delivery_seller")
    output, _ = run_workflow(tmp_path, case, data)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    flow = _CaseWorkflow(
        case, FakeGateway(data, case["case_id"]), TraceWriter(tmp_path / "verify.jsonl", contracts)
    )
    flow.evidence = {
        ref: {"evidence_ref": ref, "data": list(data.values())} for ref in output["evidence_refs"]
    }
    if problem == "claim":
        output["claim_assessments"][0]["claim_id"] = "unknown-claim"
    elif problem == "entity":
        output["affected_entities"]["order_ids"] = ["another-order"]
    elif problem == "ref":
        output["evidence_refs"].append("ev_" + "x" * 24)
    else:
        output["root_cause_analysis"]["responsible_parties"] = [
            dict(party_type="logistics_provider", party_id=None)
        ]
    with pytest.raises(ValueError, match="Verifier rejected"):
        flow.verify(output, data["get_policy"]["rules"]["late_delivery_seller"])
