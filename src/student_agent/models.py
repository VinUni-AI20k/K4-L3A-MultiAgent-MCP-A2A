from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HandoffEnvelope:
    case_id: str
    source_actor: str
    target_actor: str
    handoff_type: str  # task_dispatch | context_return | verdict_handoff
    payload: dict[str, Any]
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class OrderContext:
    order_id: str
    status: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    sellers: list[dict[str, Any]] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    order_ref: str | None = None
    items_ref: str | None = None
    sellers_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class PaymentContext:
    order_id: str
    payment_references: list[str] = field(default_factory=list)
    payments: list[dict[str, Any]] = field(default_factory=list)
    payment_timeline: list[dict[str, Any]] = field(default_factory=list)
    refund_timeline: list[dict[str, Any]] = field(default_factory=list)
    total_paid_brl: float = 0.0
    split_payment: bool = False
    payments_ref: str | None = None
    timeline_ref: str | None = None
    refund_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ShipmentContext:
    order_id: str
    shipment_ids: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    carrier: str | None = None
    estimated_delivery: str | None = None
    delivered_date: str | None = None
    is_late: bool = False
    delay_days: int = 0
    shipment_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class PolicyVerdict:
    primary_issue: str
    case_status: str
    confidence: float
    ranked_causes: list[dict[str, Any]]
    responsible_parties: list[dict[str, Any]]
    claim_assessments: list[dict[str, Any]]
    recommended_refund_brl: float
    refund_lines: list[dict[str, Any]]
    resolution_actions: list[str]
    data_conflicts: list[dict[str, Any]]
    policy_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
