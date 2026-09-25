from __future__ import annotations

from typing import Any

import pytest

from student_agent.agents.payment_policy import PaymentPolicyAgent


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        evidence_ref = f"ev_{tool_name}_0123456789"
        fixtures: dict[str, tuple[str, Any]] = {
            "get_order_payments": (
                "payment",
                {
                    "payments": [
                        {
                            "payment_reference": "PAY-001",
                            "payment_type": "credit_card",
                        }
                    ]
                },
            ),
            "get_payment_timeline": (
                "payment",
                {"events": [{"status": "captured", "is_duplicate": True}]},
            ),
            "get_refund_timeline": (
                "refund",
                {"events": [{"status": "pending"}]},
            ),
            "get_policy": (
                "policy",
                {"policy_version": "POLICY-V1", "rules": []},
            ),
        }
        domain, data = fixtures[tool_name]
        return {
            "domain": domain,
            "evidence_ref": evidence_ref,
            "data": data,
        }


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> None:
        self.events.append(event)


@pytest.mark.anyio
async def test_payment_agent_calls_scoped_tools_and_preserves_evidence() -> None:
    gateway = FakeGateway()
    trace = FakeTrace()
    agent = PaymentPolicyAgent(gateway, trace)  # type: ignore[arg-type]

    result = await agent.run(
        "CASE_001",
        {
            "case_info": {
                "case_id": "CASE_001",
                "customer_message": "My order ORDER-FROM-TEXT was charged twice",
                "policy_version": "POLICY-V1",
                "claims": [
                    {
                        "claim_id": "C1",
                        "claim_type": "duplicate_charge",
                        "order_id": "ORDER-001",
                    }
                ],
            }
        },
    )

    assert gateway.calls == [
        ("get_order_payments", "CASE_001", {"order_id": "ORDER-001"}),
        ("get_payment_timeline", "CASE_001", {"order_id": "ORDER-001"}),
        ("get_refund_timeline", "CASE_001", {"order_id": "ORDER-001"}),
        ("get_policy", "CASE_001", {"policy_version": "POLICY-V1"}),
    ]
    assert result["status"] == "completed"
    assert result["payment_references"] == ["PAY-001"]
    assert len(result["evidence_refs"]) == 4
    assert result["issue_signals"] == {
        "duplicate_charge": True,
        "refund_pending": True,
        "refund_failed": False,
    }
    tool_events = [
        event for event in trace.events if event["event_type"] == "tool_result_consumed"
    ]
    assert [event["tool_name"] for event in tool_events] == [
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ]
    assert trace.events[-1]["event_type"] == "policy_decided"
    assert result["policy"]["status"] == "evidence_collected"
    assert result["claim_assessments"] == [
        {
            "claim_id": "C1",
            "verdict": "supported",
            "confidence": 0.9,
            "evidence_refs": [
                "ev_get_order_payments_0123456789",
                "ev_get_payment_timeline_0123456789",
            ],
        }
    ]


@pytest.mark.anyio
async def test_payment_agent_does_not_treat_customer_text_as_order_id() -> None:
    gateway = FakeGateway()
    trace = FakeTrace()
    agent = PaymentPolicyAgent(gateway, trace)  # type: ignore[arg-type]

    result = await agent.run(
        "CASE_002",
        {
            "case_info": {
                "case_id": "CASE_002",
                "customer_message": "Please check ORDER-ONLY-IN-FREE-TEXT",
                "claims": [],
            }
        },
    )

    assert result["status"] == "missing_order_id"
    assert gateway.calls == []
    assert [event["event_type"] for event in trace.events] == ["policy_decided"]
    assert result["policy"]["status"] == "missing_policy_evidence"


@pytest.mark.anyio
async def test_policy_can_be_collected_without_payment_order() -> None:
    gateway = FakeGateway()
    trace = FakeTrace()
    agent = PaymentPolicyAgent(gateway, trace)  # type: ignore[arg-type]

    result = await agent.run(
        "CASE_003",
        {
            "case_info": {
                "case_id": "CASE_003",
                "policy_version": "POLICY-V1",
                "claims": [],
            }
        },
    )

    assert gateway.calls == [
        ("get_policy", "CASE_003", {"policy_version": "POLICY-V1"})
    ]
    assert result["status"] == "missing_order_id"
    assert result["policy"]["data"] == {
        "policy_version": "POLICY-V1",
        "rules": [],
    }
    assert trace.events[0]["actor"] == "policy_agent"
