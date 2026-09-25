"""Internal A2A message contracts (ARCHITECTURE.md section 3).

These models never appear in public output, trace or manifest. Every agent receives an
``AgentTask`` from the coordinator and answers with exactly one ``AgentResult``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ENTITY_KEYS: tuple[str, ...] = (
    "order_ids",
    "item_ids",
    "seller_ids",
    "payment_references",
    "shipment_ids",
)

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"
DOMAIN_AGENTS: tuple[str, ...] = (ORDER_AGENT, PAYMENT_AGENT, SHIPMENT_AGENT)

TaskType = Literal[
    "investigate_order",
    "investigate_payment",
    "investigate_shipment",
    "apply_policy",
    "verify",
]
ResultStatus = Literal["completed", "needs_handoff", "insufficient_evidence", "failed"]
RESULT_STATUSES: frozenset[str] = frozenset(
    {"completed", "needs_handoff", "insufficient_evidence", "failed"}
)

# actor -> the only task type the coordinator may assign to it
TASK_TYPE_BY_AGENT: dict[str, str] = {
    ORDER_AGENT: "investigate_order",
    PAYMENT_AGENT: "investigate_payment",
    SHIPMENT_AGENT: "investigate_shipment",
    POLICY_AGENT: "apply_policy",
    VERIFIER: "verify",
}


def _unique_sorted(values: Any) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if value not in (None, "")}))


@dataclass(frozen=True)
class EntityIds:
    order_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    seller_ids: tuple[str, ...] = ()
    payment_references: tuple[str, ...] = ()
    shipment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for key in ENTITY_KEYS:
            object.__setattr__(self, key, _unique_sorted(getattr(self, key)))

    def union(self, other: EntityIds) -> EntityIds:
        return EntityIds(**{key: getattr(self, key) + getattr(other, key) for key in ENTITY_KEYS})

    def is_empty(self) -> bool:
        return not any(getattr(self, key) for key in ENTITY_KEYS)

    def to_public(self) -> dict[str, list[str]]:
        return {key: list(getattr(self, key)) for key in ENTITY_KEYS}


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str


@dataclass(frozen=True)
class Finding:
    finding_id: str
    finding_code: str
    value: Any
    entity_ids: EntityIds
    claim_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class DataConflict:
    field: str
    sources: tuple[str, ...]
    selected_source: str | None
    resolution_code: str


@dataclass(frozen=True)
class HandoffRequest:
    target: str
    task_type: str
    reason_code: str
    entity_ids: EntityIds
    claim_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainTaskPayload:
    entity_ids: EntityIds
    claims: tuple[Claim, ...]
    findings: tuple[Finding, ...]
    evidence_context: tuple[str, ...]
    # Hint codes from coordinator intake plus handoff/remediation reason codes. They
    # describe *why* the task exists; they are never business facts.
    focus: tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainResult:
    findings: tuple[Finding, ...]
    affected_entities: EntityIds
    evidence_refs: tuple[str, ...]
    conflicts: tuple[DataConflict, ...] = ()
    warnings: tuple[str, ...] = ()
    handoff_requests: tuple[HandoffRequest, ...] = ()


@dataclass(frozen=True)
class PolicyTaskPayload:
    claims: tuple[Claim, ...]
    findings: tuple[Finding, ...]
    affected_entities: EntityIds
    evidence_context: tuple[str, ...]
    conflicts: tuple[DataConflict, ...]
    coverage_gaps: tuple[str, ...]
    # Team addition: get_policy requires policy_version; None when the input has none.
    policy_version: str | None = None
    intent_hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupportLink:
    output_path: str
    finding_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class PolicyDecision:
    """Business fields follow the public output schema (plain JSON-compatible values)."""

    assessment: dict[str, Any]
    root_cause_analysis: dict[str, Any]
    financial_resolution: dict[str, Any]
    resolution_actions: tuple[str, ...]
    data_conflicts: tuple[DataConflict, ...]
    evidence_refs: tuple[str, ...]
    support_links: tuple[SupportLink, ...] = ()
    claim_assessments: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class RemediationRequest:
    target: str
    reason_code: str
    output_paths: tuple[str, ...] = ()
    required_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationTaskPayload:
    candidate_output: dict[str, Any]
    candidate_revision: int
    policy_decision: PolicyDecision
    findings: tuple[Finding, ...]
    coverage_gaps: tuple[str, ...]
    trace_snapshot: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class VerificationResult:
    candidate_revision: int
    accepted: bool
    reason_codes: tuple[str, ...] = ()
    remediation_requests: tuple[RemediationRequest, ...] = ()


TaskPayload = DomainTaskPayload | PolicyTaskPayload | VerificationTaskPayload
ResultPayload = DomainResult | PolicyDecision | VerificationResult


@dataclass(frozen=True)
class AgentTask:
    local_run_id: str
    case_id: str
    task_id: str
    sender: str
    target: str
    task_type: TaskType
    attempt: Literal[0, 1]
    payload: TaskPayload


@dataclass(frozen=True)
class AgentResult:
    local_run_id: str
    case_id: str
    task_id: str
    actor: str
    status: ResultStatus
    payload: ResultPayload | None
    error_code: str | None = None


@dataclass
class AgentContext:
    """Runtime handles given to an agent for one task. Not part of the A2A message.

    ``gateway``/``ledger`` are only meaningful for agents allowed to use them; the
    coordinator never calls MCP itself. ``repair_reason_codes`` is set on the single
    contract-repair attempt and means: fix the result shape with evidence already held,
    do not call MCP again.
    """

    local_run_id: str
    case_id: str
    trace: Any
    gateway: Any = None
    ledger: Any = None
    deadline: float = field(default_factory=lambda: time.monotonic() + 120.0)
    repair_reason_codes: tuple[str, ...] = ()

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


class Agent(Protocol):
    actor: str

    async def handle(self, task: AgentTask, context: AgentContext) -> AgentResult: ...
