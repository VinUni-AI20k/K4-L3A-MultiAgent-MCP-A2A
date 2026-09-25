from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrderLogisticsResult:
    """Kết quả bàn giao từ Thành viên 2 (Order & Logistics) sang Coordinator."""
    order_id: str
    order_status: str
    order_data: dict[str, Any] = field(default_factory=dict)
    shipment_data: dict[str, Any] = field(default_factory=dict)
    items_data: list[dict[str, Any]] = field(default_factory=list)
    
    # Thực thể trích xuất
    order_ids: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    shipment_ids: list[str] = field(default_factory=list)
    
    # Giá trị đơn tính từ items
    items_total_brl: float = 0.0
    freight_total_brl: float = 0.0
    order_total_brl: float = 0.0
    
    # Kết luận vận chuyển
    is_late: bool = False
    delay_party: str | None = None  # "seller", "logistics_provider", hoặc None
    suggested_issue: str | None = None  # "late_delivery_seller", "late_delivery_logistics", etc.
    
    # Bằng chứng
    evidence_refs: list[str] = field(default_factory=list)
    policy_ev_ref: str | None = None
    policy_rules: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentResolutionResult:
    """Kết quả bàn giao từ Thành viên 3 (Payment & Resolution) sang Coordinator."""
    payment_references: list[str] = field(default_factory=list)
    total_paid_brl: float = 0.0
    
    # Kết luận tài chính
    is_split_payment: bool = False
    is_duplicate_charge: bool = False
    is_payment_mismatch: bool = False
    refund_status: str | None = None  # "pending", "failed", "completed"
    
    suggested_issue: str | None = None  # "canceled_order_paid", "duplicate_charge", etc.
    
    # Đề xuất hoàn tiền
    recommended_refund_brl: float = 0.0
    refund_lines: list[dict[str, Any]] = field(default_factory=list)
    resolution_actions: list[str] = field(default_factory=list)
    
    # Bằng chứng
    evidence_refs: list[str] = field(default_factory=list)
    policy_ev_ref: str | None = None
    policy_rules: dict[str, Any] = field(default_factory=dict)
    
    # Đánh giá claims
    claim_assessments: list[dict[str, Any]] = field(default_factory=list)

