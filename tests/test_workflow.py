from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.model_client import DEFAULT_MODEL
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


def evidence(number: int, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{number:020d}",
        "result_hash": "sha256:" + f"{number:064x}",
        "domain": domain,
        "data": data,
    }


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        return self.responses[tool_name]


class FakeModel:
    model = DEFAULT_MODEL

    def __init__(self, issue: str) -> None:
        self.issue = issue

    async def verify_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["deterministic_candidate"]["assessment"]["primary_issue"] == self.issue
        return {"primary_issue": self.issue, "confidence": 0.93, "flags": []}


def make_case(issue: str) -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_TEST",
        "opened_at": "2018-01-01T00:00:00Z",
        "customer_request": {
            "language": "vi",
            "message": "test",
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-primary", "topic": issue},
                {"claim_id": "claim-refund", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


def policy(issue: str, *, refund: float, status: str, action: str) -> dict[str, Any]:
    return evidence(
        9,
        "policy",
        {
            "policy_version": "EC_POLICY_V1",
            "currency": "BRL",
            "rules": {
                issue: {
                    "case_status": status,
                    "recommended_action": action,
                    "refund_brl": refund,
                    "responsible_parties": [
                        {"party_type": "payment_provider", "party_id": None}
                    ],
                }
            },
        },
    )


def run_case(
    tmp_path: Path, issue: str, responses: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], FakeGateway, Path]:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = FakeGateway(responses)
    output = asyncio.run(
        solve_case(make_case(issue), gateway, trace, model=FakeModel(issue))  # type: ignore[arg-type]
    )
    contracts.validate_output(output, "test output")
    return output, gateway, trace_path


def test_canceled_paid_uses_scoped_order_payment_and_policy(tmp_path: Path) -> None:
    issue = "canceled_order_paid"
    responses = {
        "get_order": evidence(
            1, "order", {"order_id": "order-1", "order_status": "canceled"}
        ),
        "get_order_payments": evidence(
            2,
            "payment",
            [{"order_id": "order-1", "payment_value": "79.00"}],
        ),
        "get_policy": policy(issue, refund=79, status="action_required", action="issue_refund"),
    }

    output, gateway, trace_path = run_case(tmp_path, issue, responses)

    assert output["assessment"] == {
        "primary_issue": issue,
        "case_status": "action_required",
        "confidence": 0.93,
    }
    assert output["financial_resolution"]["recommended_refund_brl"] == 79
    assert [call[0] for call in gateway.calls] == [
        "get_order",
        "get_order_payments",
        "get_policy",
    ]
    trace_text = trace_path.read_text(encoding="utf-8")
    assert '"event_type":"policy_decided"' in trace_text
    assert '"event_type":"verification_completed"' in trace_text


@pytest.mark.parametrize(
    ("issue", "timeline", "refund", "status", "action"),
    [
        (
            "payment_mismatch",
            {"events": [{"event_type": "reconciliation_mismatch", "status": "open"}]},
            35,
            "action_required",
            "reconcile_payment",
        ),
        (
            "refund_pending",
            {"events": [{"event_type": "refund_requested", "status": "pending"}]},
            0,
            "needs_investigation",
            "monitor_refund",
        ),
        (
            "refund_failed",
            {"events": [{"event_type": "refund_requested", "status": "failed"}]},
            52,
            "action_required",
            "retry_refund",
        ),
    ],
)
def test_lifecycle_issues_follow_authoritative_policy(
    tmp_path: Path,
    issue: str,
    timeline: dict[str, Any],
    refund: float,
    status: str,
    action: str,
) -> None:
    timeline_tool = "get_refund_timeline" if issue.startswith("refund_") else "get_payment_timeline"
    timeline_domain = "refund" if issue.startswith("refund_") else "payment"
    responses = {
        "get_order": evidence(
            1, "order", {"order_id": "order-1", "order_status": "delivered"}
        ),
        timeline_tool: evidence(2, timeline_domain, {"order_id": "order-1", **timeline}),
        "get_policy": policy(issue, refund=refund, status=status, action=action),
    }

    output, _, _ = run_case(tmp_path, issue, responses)

    assert output["assessment"]["primary_issue"] == issue
    assert output["assessment"]["case_status"] == status
    assert output["financial_resolution"]["recommended_refund_brl"] == refund


def test_unverified_claim_degrades_to_insufficient_evidence(tmp_path: Path) -> None:
    issue = "canceled_order_paid"
    responses = {
        "get_order": evidence(
            1, "order", {"order_id": "order-1", "order_status": "delivered"}
        ),
        "get_order_payments": evidence(2, "payment", []),
        "get_policy": policy(issue, refund=79, status="action_required", action="issue_refund"),
    }
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(responses)

    output = asyncio.run(
        solve_case(
            make_case(issue),
            gateway,  # type: ignore[arg-type]
            trace,
            model=FakeModel("insufficient_evidence"),
        )
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_unsupported_claim_ignores_unrelated_payment_noise(tmp_path: Path) -> None:
    issue = "unsupported_claim"
    responses = {
        "get_order": evidence(
            1, "order", {"order_id": "order-1", "order_status": "delivered"}
        ),
        "get_payment_timeline": evidence(
            2,
            "payment",
            {
                "order_id": "order-1",
                "events": [{"event_type": "reconciliation_mismatch", "status": "open"}],
            },
        ),
        "get_shipment_summary": evidence(
            3,
            "shipment",
            {
                "order_id": "order-1",
                "order_status": "delivered",
                "delivered_customer_at": "2018-01-07T09:00:00-03:00",
                "estimated_delivery_at": "2018-01-08T09:00:00-03:00",
                "events": [
                    {
                        "event_at": "2018-05-17T09:00:00-03:00",
                        "event_type": "delivered_late",
                        "actor": "logistics_provider",
                    }
                ],
            },
        ),
        "get_policy": policy(
            issue, refund=0, status="no_action", action="document_no_action"
        ),
    }

    output, _, _ = run_case(tmp_path, issue, responses)

    assert output["assessment"]["primary_issue"] == issue
    assert output["assessment"]["case_status"] == "no_action"
