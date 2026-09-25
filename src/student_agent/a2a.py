from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Actor = Literal[
    "coordinator",
    "order-item-agent",
    "payment-agent",
    "shipment-agent",
    "policy-agent",
    "verifier",
]

MessageStatus = Literal[
    "pending",
    "completed",
    "needs_more_evidence",
]


@dataclass
class VerifiedFact:
    """Dữ kiện và bằng chứng hỗ trợ, chỉ dùng nội bộ."""

    name: str
    value: Any
    evidence_refs: list[str]


@dataclass
class AgentMessage:
    """Message giao việc hoặc chuyển kết quả giữa các agent."""

    case_id: str
    sender: Actor
    recipient: Actor
    task: str
    entity_scope: dict[str, list[str]] = field(default_factory=dict)
    facts: list[VerifiedFact] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    status: MessageStatus = "pending"