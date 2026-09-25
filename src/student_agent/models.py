from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class Assessment(BaseModel):
    primary_issue: Literal[
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
        "insufficient_evidence",
    ]
    case_status: Literal["action_required", "no_action", "needs_investigation"]
    confidence: float = Field(ge=0.0, le=1.0)


class AffectedEntities(BaseModel):
    order_ids: List[str] = Field(default_factory=list, max_length=20)
    item_ids: List[str] = Field(default_factory=list, max_length=20)
    seller_ids: List[str] = Field(default_factory=list, max_length=20)
    payment_references: List[str] = Field(default_factory=list, max_length=20)
    shipment_ids: List[str] = Field(default_factory=list, max_length=20)


class ClaimAssessment(BaseModel):
    claim_id: str
    verdict: Literal["supported", "unsupported", "partially_supported", "insufficient_evidence"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: List[str] = Field(default_factory=list, max_length=30)


class RankedCause(BaseModel):
    cause_code: str
    rank: int = Field(ge=1, le=5)


class ResponsibleParty(BaseModel):
    party_type: Literal[
        "seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"
    ]
    party_id: Optional[str] = None


class RootCauseAnalysis(BaseModel):
    ranked_causes: List[RankedCause] = Field(default_factory=list, max_length=5)
    responsible_parties: List[ResponsibleParty] = Field(default_factory=list, max_length=5)


class DataConflict(BaseModel):
    field: str
    sources: List[str] = Field(min_length=2, max_length=5)
    selected_source: Optional[str] = None
    resolution_code: str


class RefundLine(BaseModel):
    reason_code: str
    amount_brl: float = Field(ge=0.0)
    entity_id: Optional[str] = None


class FinancialResolution(BaseModel):
    currency: Literal["BRL"] = "BRL"
    recommended_refund_brl: float = Field(ge=0.0)
    refund_lines: List[RefundLine] = Field(default_factory=list, max_length=10)


class L3AOutput(BaseModel):
    schema_version: Literal["day09-l3a-output-v2"] = "day09-l3a-output-v2"
    case_id: str
    assessment: Assessment
    affected_entities: AffectedEntities
    claim_assessments: Optional[List[ClaimAssessment]] = Field(default=None, max_length=5)
    root_cause_analysis: RootCauseAnalysis
    evidence_refs: List[str] = Field(default_factory=list, max_length=30)
    data_conflicts: List[DataConflict] = Field(default_factory=list, max_length=5)
    financial_resolution: FinancialResolution
    resolution_actions: List[str] = Field(default_factory=list, max_length=8)
