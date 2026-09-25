from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .evidence_rules import WINDOW_SLACK, parse_ts

if TYPE_CHECKING:
    from .policy_agent import PolicyDecision

EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


@dataclass(frozen=True)
class EvidenceRecord:
    """Where one evidence_ref came from. Only refs recorded here may be cited."""

    ref: str
    domain: str
    tool_name: str
    actor: str
    case_id: str
    warnings: tuple[str, ...] = ()


@dataclass
class CaseState:
    """Shared A2A state handed between agents for exactly one case."""

    case_id: str
    opened_at: str
    customer_request: dict[str, Any]
    policy_version: str

    evidence_refs: list[str] = field(default_factory=list)
    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)

    order_data: dict[str, Any] | None = None
    shipment_data: dict[str, Any] | None = None
    payment_data: dict[str, Any] | None = None
    policy_data: dict[str, Any] | None = None
    # Real sellers of this case, taken from in-window item/shipment evidence.
    seller_ids: list[str] = field(default_factory=list)

    claims_assessed: list[dict[str, Any]] = field(default_factory=list)
    conflicts_detected: list[dict[str, Any]] = field(default_factory=list)
    primary_issue_candidate: str | None = None
    financial_resolution_candidate: dict[str, Any] | None = None
    policy_decision: PolicyDecision | None = None

    @classmethod
    def from_case(cls, case: dict[str, Any]) -> CaseState:
        return cls(
            case_id=case["case_id"],
            opened_at=case.get("opened_at", ""),
            customer_request=case.get("customer_request") or {},
            policy_version=case.get("policy_version", ""),
        )

    @property
    def claimed_order_id(self) -> str | None:
        return self.customer_request.get("claimed_order_id")

    @property
    def claims(self) -> list[dict[str, Any]]:
        claims = self.customer_request.get("claims") or []
        return [claim for claim in claims if isinstance(claim, dict) and claim.get("claim_id")]

    def case_seller_ids(self) -> list[str]:
        """Sellers that evidence ties to this case; never the example seller from the policy.

        Uses ``seller_ids`` when a caller set it, otherwise the ``shipping_limits`` rows of
        ``shipment_data`` inside ``[order_purchase_timestamp - 1 day, opened_at]``.
        """
        explicit = [s for s in self.seller_ids if isinstance(s, str) and s]
        if explicit:
            return list(dict.fromkeys(explicit))
        limits = (self.shipment_data or {}).get("shipping_limits")
        end = parse_ts(self.opened_at)
        purchase = parse_ts((self.order_data or {}).get("order_purchase_timestamp"))
        start = purchase - WINDOW_SLACK if purchase else None
        sellers: list[str] = []
        for row in limits if isinstance(limits, list) else []:
            if not isinstance(row, dict):
                continue
            seller = row.get("seller_id")
            deadline = parse_ts(row.get("shipping_limit_at"))
            in_window = (
                deadline is not None
                and end is not None
                and deadline <= end
                and (start is None or deadline >= start)
            )
            if isinstance(seller, str) and seller and in_window and seller not in sellers:
                sellers.append(seller)
        return sellers

    async def fetch(
        self,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        *,
        tool_name: str,
        actor: str,
        **arguments: str,
    ) -> dict[str, Any]:
        """Call an MCP tool scoped to this case and record the evidence it returns."""
        evidence = await gateway.call(tool_name, case_id=self.case_id, **arguments)
        self.consume(evidence, tool_name=tool_name, actor=actor, trace=trace)
        return evidence

    def consume(
        self, evidence: dict[str, Any], *, tool_name: str, actor: str, trace: TraceWriter
    ) -> str:
        """Register evidence and emit the tool_result_consumed event that links it to the trace."""
        ref = evidence.get("evidence_ref")
        if not isinstance(ref, str) or not EVIDENCE_REF_PATTERN.fullmatch(ref):
            raise ValueError(f"{tool_name} returned an invalid evidence_ref")
        if ref not in self.evidence:
            self.evidence[ref] = EvidenceRecord(
                ref=ref,
                domain=str(evidence.get("domain", "")),
                tool_name=tool_name,
                actor=actor,
                case_id=self.case_id,
                warnings=tuple(evidence.get("warnings") or ()),
            )
            self.evidence_refs.append(ref)
        trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ref],
        )
        return ref

    def refs_for_domain(self, domain: str) -> list[str]:
        return [ref for ref in self.evidence_refs if self.evidence[ref].domain == domain]
