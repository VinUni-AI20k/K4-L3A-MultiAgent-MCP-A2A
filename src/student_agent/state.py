from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import asyncio

from .a2a import Actor, AgentMessage


TOOL_PERMISSIONS: dict[Actor, frozenset[str]] = {
    "coordinator": frozenset(),
    "order-item-agent": frozenset({
        "get_order",
        "get_order_items",
        "get_sellers",
        "get_product_context",
    }),
    "payment-agent": frozenset({
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
    }),
    "shipment-agent": frozenset({
        "get_shipment_summary",
    }),
    "policy-agent": frozenset({
        "get_policy",
    }),
    "verifier": frozenset(),
}


def check_tool_permission(actor: Actor, tool_name: str) -> None:
    """Từ chối actor không được phép gọi tool dữ liệu."""
    allowed = TOOL_PERMISSIONS.get(actor, frozenset())
    if tool_name not in allowed:
        raise PermissionError(
            f"Actor {actor!r} cannot call tool {tool_name!r}"
        )


@dataclass
class EvidenceRecord:
    """Giữ envelope và ngữ cảnh gọi riêng biệt."""

    case_id: str
    actor: Actor
    tool_name: str
    arguments: dict[str, str]
    envelope: dict[str, Any]


@dataclass
class CaseState:
    """Mỗi lần solve_case phải tạo một instance mới."""

    case_id: str
    entity_scope: dict[str, list[str]]
    policy_version: str
    deadline: float

    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)
    messages: list[AgentMessage] = field(default_factory=list)
    rework_rounds: int = 0

def check_tool_request(
    state: CaseState,
    actor: Actor,
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    """Kiểm tra quyền, arguments và phạm vi trước khi gọi MCP."""

    check_tool_permission(actor, tool_name)

    if tool_name == "get_policy":
        expected = {"policy_version"}
    else:
        expected = {"order_id"}

    if set(arguments) != expected:
        raise ValueError(
            f"{tool_name}: expected arguments {sorted(expected)}"
        )

    for name, value in arguments.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{tool_name}: invalid {name}")

    if tool_name == "get_policy":
        if arguments["policy_version"] != state.policy_version:
            raise ValueError("Policy version outside case scope")
    else:
        allowed_orders = state.entity_scope.get("order_ids", [])
        if arguments["order_id"] not in allowed_orders:
            raise ValueError("Order outside case scope")

def create_case_state(case: dict[str, Any]) -> CaseState:
    """Khởi tạo state cho cấu trúc input một đơn đã quan sát."""

    def require_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        return value

    case_id = require_text(case.get("case_id"), "case_id")
    policy_version = require_text(
        case.get("policy_version"), "policy_version"
    )

    request = case.get("customer_request")
    if not isinstance(request, dict):
        raise ValueError("customer_request must be an object")

    order_id = require_text(
        request.get("claimed_order_id"),
        "customer_request.claimed_order_id",
    )

    claims = request.get("claims")
    if not isinstance(claims, list):
        raise ValueError("customer_request.claims must be an array")

    claim_ids: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError("Each claim must be an object")

        claim_id = require_text(claim.get("claim_id"), "claim_id")
        require_text(claim.get("topic"), "claim.topic")

        if claim_id in claim_ids:
            raise ValueError("Duplicate claim_id")
        claim_ids.add(claim_id)

    return CaseState(
        case_id=case_id,
        entity_scope={"order_ids": [order_id]},
        policy_version=policy_version,
        deadline=asyncio.get_running_loop().time() + 180.0,
    )