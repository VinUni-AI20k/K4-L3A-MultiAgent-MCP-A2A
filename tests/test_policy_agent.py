"""Unit tests cho PolicyAgent — dùng mock data, không cần MCP thật.

Người sở hữu: Nguyên
Chạy: pytest tests/test_policy_agent.py -v
"""

from __future__ import annotations

import pytest

from student_agent.agents.policy_agent import (
    ClaimAssessment,
    PolicyAgent,
    PolicyResult,
    VALID_PRIMARY_ISSUES,
    VALID_VERDICTS,
    _clamp,
    _unique_refs,
)


# --------------------------------------------------------------------------- #
# Mock classes                                                                 #
# --------------------------------------------------------------------------- #

def _make_evidence_ref(domain: str) -> str:
    """Tạo evidence_ref hợp lệ cho testing."""
    return f"ev_mock_{domain}_test_abcdef1234567890"


class MockGateway:
    """Mock EvidenceGateway — trả policy data giả."""

    def __init__(self, policy_data: dict | None = None):
        self.calls: list[dict] = []
        self._policy_data = policy_data or {
            "policy_version": "EC_POLICY_V1",
            "refund_eligible_statuses": ["canceled", "unavailable"],
            "late_delivery_threshold_days": 7,
        }

    async def call(self, tool_name: str, *, case_id: str, **kwargs) -> dict:
        self.calls.append({"tool_name": tool_name, "case_id": case_id, **kwargs})
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": _make_evidence_ref("policy"),
            "result_hash": "sha256:" + "a" * 64,
            "domain": "policy",
            "data": self._policy_data,
        }


class MockTrace:
    """Mock TraceWriter — ghi vào list thay vì file."""

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, **kwargs) -> dict:
        self.events.append(kwargs)
        return {"event_id": "evt_mock_test_1234567890ab"}


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

def _make_findings(
    *,
    order_status: str = "canceled",
    payment_value: float = 120.50,
    order_delivered_customer_date: str = "",
    order_estimated_delivery_date: str = "",
    order_delivered_carrier_date: str = "",
    shipping_limit_date: str = "",
    price: float = 100.0,
    freight_value: float = 20.50,
) -> dict:
    """Tạo findings giả với các tham số tùy chỉnh."""
    return {
        "order": {
            "data": {
                "order_id": "test_order_001",
                "order_status": order_status,
                "price": price,
                "freight_value": freight_value,
                "order_estimated_delivery_date": order_estimated_delivery_date,
                "order_delivered_customer_date": order_delivered_customer_date,
                "order_delivered_carrier_date": order_delivered_carrier_date,
            },
            "evidence_ref": _make_evidence_ref("order"),
        },
        "payment": {
            "data": {
                "payment_type": "credit_card",
                "payment_value": payment_value,
            },
            "evidence_ref": _make_evidence_ref("payment"),
        },
        "shipment": {
            "data": {
                "shipping_limit_date": shipping_limit_date,
                "carrier": "test_carrier",
            },
            "evidence_ref": _make_evidence_ref("shipment"),
        },
    }


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_canceled_order_paid_supported():
    """Order canceled + payment exists → verdict = supported."""
    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    result = await agent.evaluate(
        case_id="L3A_CASE_TEST",
        policy_version="EC_POLICY_V1",
        claims=[{"claim_id": "claim-t-a", "topic": "canceled_order_paid"}],
        findings=_make_findings(order_status="canceled", payment_value=100.0),
    )

    assert isinstance(result, PolicyResult)
    assert len(result.claim_assessments) == 1
    assert result.claim_assessments[0].verdict == "supported"
    assert result.primary_issue == "canceled_order_paid"
    assert result.case_status == "action_required"


@pytest.mark.asyncio
async def test_canceled_order_paid_unsupported():
    """Order delivered (không phải canceled) → verdict = unsupported."""
    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    result = await agent.evaluate(
        case_id="L3A_CASE_TEST",
        policy_version="EC_POLICY_V1",
        claims=[{"claim_id": "claim-t-a", "topic": "canceled_order_paid"}],
        findings=_make_findings(order_status="delivered", payment_value=100.0),
    )

    assert result.claim_assessments[0].verdict == "unsupported"
    assert result.case_status == "no_action"


@pytest.mark.asyncio
async def test_late_delivery_seller_fault():
    """Seller shipped late → late_delivery_seller supported."""
    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    result = await agent.evaluate(
        case_id="L3A_CASE_TEST",
        policy_version="EC_POLICY_V1",
        claims=[{"claim_id": "claim-t-a", "topic": "late_delivery_seller"}],
        findings=_make_findings(
            order_status="delivered",
            order_estimated_delivery_date="2018-01-10",
            order_delivered_customer_date="2018-01-20",
            order_delivered_carrier_date="2018-01-12",
            shipping_limit_date="2018-01-08",
        ),
    )

    assert result.claim_assessments[0].verdict == "supported"
    assert result.primary_issue == "late_delivery_seller"


@pytest.mark.asyncio
async def test_late_delivery_logistics_fault():
    """Seller shipped on time, carrier late → late_delivery_logistics supported."""
    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    result = await agent.evaluate(
        case_id="L3A_CASE_TEST",
        policy_version="EC_POLICY_V1",
        claims=[{"claim_id": "claim-t-a", "topic": "late_delivery_logistics"}],
        findings=_make_findings(
            order_status="delivered",
            order_estimated_delivery_date="2018-01-10",
            order_delivered_customer_date="2018-01-20",
            order_delivered_carrier_date="2018-01-07",  # seller shipped TRƯỚC deadline
            shipping_limit_date="2018-01-08",
        ),
    )

    assert result.claim_assessments[0].verdict == "supported"
    assert result.primary_issue == "late_delivery_logistics"


@pytest.mark.asyncio
async def test_payment_mismatch():
    """Payment amount ≠ order value → payment_mismatch supported."""
    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    result = await agent.evaluate(
        case_id="L3A_CASE_TEST",
        policy_version="EC_POLICY_V1",
        claims=[{"claim_id": "claim-t-a", "topic": "payment_mismatch"}],
        findings=_make_findings(
            order_status="delivered",
            price=100.0,
            freight_value=20.0,
            payment_value=50.0,  # Khác với order value (120)
        ),
    )

    assert result.claim_assessments[0].verdict == "supported"
    assert result.primary_issue == "payment_mismatch"


@pytest.mark.asyncio
async def test_insufficient_evidence():
    """Missing critical data → verdict = insufficient_evidence."""
    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    result = await agent.evaluate(
        case_id="L3A_CASE_TEST",
        policy_version="EC_POLICY_V1",
        claims=[{"claim_id": "claim-t-a", "topic": "canceled_order_paid"}],
        findings={},  # Không có findings nào
    )

    assert result.claim_assessments[0].verdict == "insufficient_evidence"
    assert result.case_status == "needs_investigation"


@pytest.mark.asyncio
async def test_confidence_always_in_range():
    """Confidence luôn nằm trong [0, 1] dù input nào."""
    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    topics = [
        "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
        "late_delivery_logistics", "payment_mismatch", "duplicate_charge",
        "refund_pending", "refund_failed", "requested_full_refund",
        "unknown_topic_xyz",
    ]

    for topic in topics:
        result = await agent.evaluate(
            case_id="L3A_CASE_TEST",
            policy_version="EC_POLICY_V1",
            claims=[{"claim_id": f"claim-{topic}", "topic": topic}],
            findings=_make_findings(),
        )
        assert 0.0 <= result.confidence <= 1.0, f"confidence out of range for topic {topic}"
        for ca in result.claim_assessments:
            assert 0.0 <= ca.confidence <= 1.0, f"claim confidence out of range for {topic}"
            assert ca.verdict in VALID_VERDICTS, f"invalid verdict for {topic}"


# --------------------------------------------------------------------------- #
# Tests cho trace emit                                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_trace_events_emitted():
    """Verify trace events 'tool_result_consumed' và 'policy_decided' được emit."""
    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    await agent.evaluate(
        case_id="L3A_CASE_TEST",
        policy_version="EC_POLICY_V1",
        claims=[{"claim_id": "claim-t-a", "topic": "canceled_order_paid"}],
        findings=_make_findings(),
    )

    event_types = [e["event_type"] for e in trace.events]
    assert "tool_result_consumed" in event_types, "Phải emit tool_result_consumed khi gọi get_policy"
    assert "policy_decided" in event_types, "Phải emit policy_decided khi ra quyết định"

    # Verify actor
    for event in trace.events:
        assert event["actor"] == "policy-agent"


@pytest.mark.asyncio
async def test_evidence_refs_are_valid():
    """Tất cả evidence_refs phải match pattern ^ev_[A-Za-z0-9_-]{20,96}$."""
    import re
    pattern = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")

    gateway = MockGateway()
    trace = MockTrace()
    agent = PolicyAgent(gateway, trace)

    result = await agent.evaluate(
        case_id="L3A_CASE_TEST",
        policy_version="EC_POLICY_V1",
        claims=[{"claim_id": "claim-t-a", "topic": "canceled_order_paid"}],
        findings=_make_findings(),
    )

    for ref in result.evidence_refs:
        assert pattern.match(ref), f"Invalid evidence_ref format: {ref}"
    for ca in result.claim_assessments:
        for ref in ca.evidence_refs:
            assert pattern.match(ref), f"Invalid claim evidence_ref format: {ref}"


# --------------------------------------------------------------------------- #
# Tests cho helper functions                                                   #
# --------------------------------------------------------------------------- #

def test_clamp():
    assert _clamp(0.5) == 0.5
    assert _clamp(-0.1) == 0.0
    assert _clamp(1.5) == 1.0
    assert _clamp(0.0) == 0.0
    assert _clamp(1.0) == 1.0


def test_unique_refs():
    assert _unique_refs(["a", "b", "a", "c"]) == ["a", "b", "c"]
    assert _unique_refs([]) == []
    assert _unique_refs(["x"]) == ["x"]
