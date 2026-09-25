from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse_timestamp(val: Any) -> datetime | None:
    """Safely parse various datetime string formats or timestamps into a datetime object."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val)
        except Exception:
            return None
    s = str(val).strip()
    if not s:
        return None

    # Try ISO format
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _is_later(date_a_raw: Any, date_b_raw: Any) -> bool:
    """Return True if date_a > date_b safely."""
    dt_a = _parse_timestamp(date_a_raw)
    dt_b = _parse_timestamp(date_b_raw)
    if dt_a is not None and dt_b is not None:
        # Normalize timezone awareness for comparison if one is naive and other aware
        if dt_a.tzinfo is not None and dt_b.tzinfo is None:
            dt_b = dt_b.replace(tzinfo=dt_a.tzinfo)
        elif dt_a.tzinfo is None and dt_b.tzinfo is not None:
            dt_a = dt_a.replace(tzinfo=dt_b.tzinfo)
        return dt_a > dt_b
    if date_a_raw and date_b_raw:
        return str(date_a_raw) > str(date_b_raw)
    return False


async def check_order_and_delivery(
    case_id: str,
    order_id: str | None,
    gateway: Any,
    trace: Any,
) -> dict[str, Any]:
    """Investigate order status and shipment delivery times.

    Returns structured findings including primary issue, responsible party,
    collected evidence references, and affected entity identifiers.
    """
    ev_list: list[str] = []
    seller_ids: list[str] = []
    item_ids: list[str] = []
    shipment_ids: list[str] = []

    if not order_id:
        return {
            "issue": "insufficient_evidence",
            "responsible": "unknown",
            "responsible_party_id": None,
            "ev": [],
            "status": "unknown",
            "order_id": None,
            "seller_ids": [],
            "item_ids": [],
            "shipment_ids": [],
            "order_data": {},
            "shipment_data": {},
        }

    # ---------------------------------------------------------
    # 1. Investigate Order details via get_order tool
    # ---------------------------------------------------------
    order_res = await gateway.call("get_order", case_id=case_id, order_id=order_id)
    ev_order = order_res.get("evidence_ref")
    if ev_order:
        ev_list.append(ev_order)
    order_data: dict[str, Any] = order_res.get("data", {})

    # Emit tool_result_consumed trace event
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="order-agent",
        tool_name="get_order",
        evidence_refs=[ev_order] if ev_order else [],
    )

    # Extract entities from order data if available
    if "seller_id" in order_data and order_data["seller_id"]:
        seller_ids.append(str(order_data["seller_id"]))
    if "items" in order_data and isinstance(order_data["items"], list):
        for item in order_data["items"]:
            if isinstance(item, dict):
                if item.get("seller_id"):
                    seller_ids.append(str(item["seller_id"]))
                if item.get("order_item_id") or item.get("item_id"):
                    item_ids.append(str(item.get("order_item_id") or item.get("item_id")))

    status = str(order_data.get("order_status", "")).lower()

    # Case A: Order was canceled by platform/seller
    if status == "canceled":
        return {
            "issue": "canceled_order_paid",
            "responsible": "platform",
            "responsible_party_id": None,
            "ev": ev_list,
            "status": status,
            "order_id": order_id,
            "seller_ids": list(dict.fromkeys(seller_ids)),
            "item_ids": list(dict.fromkeys(item_ids)),
            "shipment_ids": list(dict.fromkeys(shipment_ids)),
            "order_data": order_data,
            "shipment_data": {},
        }

    # Case B: Order item unavailable (out of stock)
    if status == "unavailable":
        seller_party_id = seller_ids[0] if seller_ids else None
        return {
            "issue": "unavailable_order_paid",
            "responsible": "seller",
            "responsible_party_id": seller_party_id,
            "ev": ev_list,
            "status": status,
            "order_id": order_id,
            "seller_ids": list(dict.fromkeys(seller_ids)),
            "item_ids": list(dict.fromkeys(item_ids)),
            "shipment_ids": list(dict.fromkeys(shipment_ids)),
            "order_data": order_data,
            "shipment_data": {},
        }

    # ---------------------------------------------------------
    # 2. Investigate Shipment timeline via get_shipment tool
    # ---------------------------------------------------------
    ship_res = await gateway.call("get_shipment", case_id=case_id, order_id=order_id)
    ev_ship = ship_res.get("evidence_ref")
    if ev_ship:
        ev_list.append(ev_ship)
    ship_data: dict[str, Any] = ship_res.get("data", {})

    # Emit tool_result_consumed trace event
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="shipment-agent",
        tool_name="get_shipment",
        evidence_refs=[ev_ship] if ev_ship else [],
    )

    # Extract entities from shipment data
    if "shipment_id" in ship_data and ship_data["shipment_id"]:
        shipment_ids.append(str(ship_data["shipment_id"]))
    if "tracking_number" in ship_data and ship_data["tracking_number"]:
        shipment_ids.append(str(ship_data["tracking_number"]))
    if "seller_id" in ship_data and ship_data["seller_id"]:
        seller_ids.append(str(ship_data["seller_id"]))

    carrier_date = ship_data.get("order_delivered_carrier_date")
    limit_date = ship_data.get("shipping_limit_date")
    delivered_customer = ship_data.get("order_delivered_customer_date")
    estimated_date = ship_data.get("order_estimated_delivery_date")

    primary_seller_id = seller_ids[0] if seller_ids else None

    # Case C: Seller handed over to carrier after shipping_limit_date
    if carrier_date and limit_date and _is_later(carrier_date, limit_date):
        return {
            "issue": "late_delivery_seller",
            "responsible": "seller",
            "responsible_party_id": primary_seller_id,
            "ev": ev_list,
            "status": status,
            "order_id": order_id,
            "seller_ids": list(dict.fromkeys(seller_ids)),
            "item_ids": list(dict.fromkeys(item_ids)),
            "shipment_ids": list(dict.fromkeys(shipment_ids)),
            "order_data": order_data,
            "shipment_data": ship_data,
        }

    # Case D: Carrier delivered to customer after order_estimated_delivery_date
    if delivered_customer and estimated_date and _is_later(delivered_customer, estimated_date):
        return {
            "issue": "late_delivery_logistics",
            "responsible": "logistics_provider",
            "responsible_party_id": None,
            "ev": ev_list,
            "status": status,
            "order_id": order_id,
            "seller_ids": list(dict.fromkeys(seller_ids)),
            "item_ids": list(dict.fromkeys(item_ids)),
            "shipment_ids": list(dict.fromkeys(shipment_ids)),
            "order_data": order_data,
            "shipment_data": ship_data,
        }

    # Case E: No order or delivery delay issue found
    return {
        "issue": "no_issue",
        "responsible": "platform",
        "responsible_party_id": None,
        "ev": ev_list,
        "status": status,
        "order_id": order_id,
        "seller_ids": list(dict.fromkeys(seller_ids)),
        "item_ids": list(dict.fromkeys(item_ids)),
        "shipment_ids": list(dict.fromkeys(shipment_ids)),
        "order_data": order_data,
        "shipment_data": ship_data,
    }
