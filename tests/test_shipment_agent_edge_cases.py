from student_agent.agents.shipment_agent import assess_shipment


def test_missing_delivery_time_returns_insufficient_evidence() -> None:
    order = {
        "order_estimated_delivery_date": "2018-01-10T00:00:00-03:00",
    }

    result = assess_shipment(order, {})

    assert result["signal"] == "insufficient_evidence"
    assert result["late"] is False
    assert result["responsible_party"] is None


def test_invalid_date_returns_insufficient_evidence() -> None:
    order = {
        "order_estimated_delivery_date": "not-a-valid-date",
        "order_delivered_customer_date": "2018-01-20T00:00:00-03:00",
    }

    result = assess_shipment(order, {})

    assert result["signal"] == "insufficient_evidence"
    assert result["late"] is False


def test_nested_shipment_data_preserves_tracking_id() -> None:
    order = {
        "dates": {
            "estimated_delivery_at": "2018-01-10T00:00:00Z",
            "delivered_at": "2018-01-20T00:00:00Z",
        }
    }
    shipment = {
        "details": {
            "tracking_id": "BR-TRACK-001",
        }
    }

    result = assess_shipment(order, shipment)

    assert result["signal"] == "late_delivery_logistics"
    assert result["late"] is True
    assert result["responsible_party"] == "logistics_provider"
    assert result["shipment_ids"] == ["BR-TRACK-001"]


def test_delivery_exactly_on_deadline_is_on_time() -> None:
    order = {
        "estimated_delivery_at": "2018-01-10T00:00:00Z",
        "delivered_at": "2018-01-10T00:00:00Z",
    }

    result = assess_shipment(order, {})

    assert result["signal"] == "on_time"
    assert result["late"] is False
    assert result["responsible_party"] is None