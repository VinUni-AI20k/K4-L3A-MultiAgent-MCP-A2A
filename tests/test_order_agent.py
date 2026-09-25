from __future__ import annotations

import asyncio
from typing import Any

from student_agent.order_agent import (
    _is_later,
    _parse_timestamp,
    check_order_and_delivery,
)


class MockGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool_name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((tool_name, kwargs))
        if tool_name in self.responses:
            return self.responses[tool_name]
        return {"evidence_ref": f"ev_mock_{tool_name}", "data": {}}


class MockTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return kwargs


def test_parse_timestamp_formats() -> None:
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("   ") is None

    # Standard formats
    dt1 = _parse_timestamp("2018-05-09 15:40:00")
    assert dt1 is not None and dt1.year == 2018 and dt1.hour == 15

    # ISO format
    dt2 = _parse_timestamp("2018-05-09T15:40:00Z")
    assert dt2 is not None and dt2.year == 2018

    # Date only
    dt3 = _parse_timestamp("2018-05-09")
    assert dt3 is not None and dt3.day == 9


def test_is_later() -> None:
    assert _is_later("2018-05-10 10:00:00", "2018-05-09 10:00:00") is True
    assert _is_later("2018-05-08 10:00:00", "2018-05-09 10:00:00") is False
    assert _is_later("2018-05-09 10:00:00", "2018-05-09 10:00:00") is False
    assert _is_later(None, "2018-05-09 10:00:00") is False
    assert _is_later("2018-05-09 10:00:00", None) is False


def test_missing_order_id() -> None:
    gateway = MockGateway({})
    trace = MockTrace()
    res = asyncio.run(check_order_and_delivery("CASE_001", None, gateway, trace))

    assert res["issue"] == "insufficient_evidence"
    assert res["responsible"] == "unknown"
    assert res["ev"] == []
    assert len(gateway.calls) == 0
    assert len(trace.events) == 0


def test_order_canceled() -> None:
    gateway = MockGateway(
        {
            "get_order": {
                "evidence_ref": "ev_order_test_canceled_12345",
                "data": {
                    "order_id": "ORD_001",
                    "order_status": "canceled",
                    "seller_id": "SEL_001",
                },
            }
        }
    )
    trace = MockTrace()
    res = asyncio.run(check_order_and_delivery("CASE_001", "ORD_001", gateway, trace))

    assert res["issue"] == "canceled_order_paid"
    assert res["responsible"] == "platform"
    assert res["status"] == "canceled"
    assert "ev_order_test_canceled_12345" in res["ev"]
    assert "SEL_001" in res["seller_ids"]
    assert len(gateway.calls) == 1

    # Verify trace emission
    assert len(trace.events) == 1
    assert trace.events[0]["event_type"] == "tool_result_consumed"
    assert trace.events[0]["actor"] == "order-agent"
    assert trace.events[0]["tool_name"] == "get_order"


def test_order_unavailable() -> None:
    gateway = MockGateway(
        {
            "get_order": {
                "evidence_ref": "ev_order_test_unavail_12345",
                "data": {
                    "order_id": "ORD_002",
                    "order_status": "unavailable",
                    "seller_id": "SEL_002",
                },
            }
        }
    )
    trace = MockTrace()
    res = asyncio.run(check_order_and_delivery("CASE_002", "ORD_002", gateway, trace))

    assert res["issue"] == "unavailable_order_paid"
    assert res["responsible"] == "seller"
    assert res["responsible_party_id"] == "SEL_002"
    assert res["status"] == "unavailable"
    assert "ev_order_test_unavail_12345" in res["ev"]


def test_late_delivery_seller() -> None:
    gateway = MockGateway(
        {
            "get_order": {
                "evidence_ref": "ev_order_test_late_seller_111",
                "data": {
                    "order_id": "ORD_003",
                    "order_status": "delivered",
                    "seller_id": "SEL_003",
                },
            },
            "get_shipment": {
                "evidence_ref": "ev_ship_test_late_seller_222",
                "data": {
                    "shipping_limit_date": "2018-05-02 12:00:00",
                    "order_delivered_carrier_date": "2018-05-05 15:00:00",  # 3 days late
                    "order_delivered_customer_date": "2018-05-10 10:00:00",
                    "order_estimated_delivery_date": "2018-05-15 00:00:00",
                    "tracking_number": "TRK_003",
                },
            },
        }
    )
    trace = MockTrace()
    res = asyncio.run(check_order_and_delivery("CASE_003", "ORD_003", gateway, trace))

    assert res["issue"] == "late_delivery_seller"
    assert res["responsible"] == "seller"
    assert res["responsible_party_id"] == "SEL_003"
    assert "ev_order_test_late_seller_111" in res["ev"]
    assert "ev_ship_test_late_seller_222" in res["ev"]
    assert "TRK_003" in res["shipment_ids"]
    assert len(trace.events) == 2


def test_late_delivery_logistics() -> None:
    gateway = MockGateway(
        {
            "get_order": {
                "evidence_ref": "ev_order_test_late_logistics_111",
                "data": {"order_id": "ORD_004", "order_status": "delivered"},
            },
            "get_shipment": {
                "evidence_ref": "ev_ship_test_late_logistics_222",
                "data": {
                    "shipping_limit_date": "2018-05-05 12:00:00",
                    "order_delivered_carrier_date": "2018-05-03 10:00:00",  # Seller was on time
                    "order_delivered_customer_date": "2018-05-20 18:00:00",  # Delivered on May 20
                    "order_estimated_delivery_date": "2018-05-15 00:00:00",  # Expected May 15
                },
            },
        }
    )
    trace = MockTrace()
    res = asyncio.run(check_order_and_delivery("CASE_004", "ORD_004", gateway, trace))

    assert res["issue"] == "late_delivery_logistics"
    assert res["responsible"] == "logistics_provider"
    assert len(res["ev"]) == 2


def test_on_time_order_no_issue() -> None:
    gateway = MockGateway(
        {
            "get_order": {
                "evidence_ref": "ev_order_test_ontime_111",
                "data": {"order_id": "ORD_005", "order_status": "delivered"},
            },
            "get_shipment": {
                "evidence_ref": "ev_ship_test_ontime_222",
                "data": {
                    "shipping_limit_date": "2018-05-05 12:00:00",
                    "order_delivered_carrier_date": "2018-05-03 10:00:00",
                    "order_delivered_customer_date": "2018-05-10 18:00:00",
                    "order_estimated_delivery_date": "2018-05-15 00:00:00",
                },
            },
        }
    )
    trace = MockTrace()
    res = asyncio.run(check_order_and_delivery("CASE_005", "ORD_005", gateway, trace))

    assert res["issue"] == "no_issue"
    assert res["responsible"] == "platform"
    assert len(res["ev"]) == 2
