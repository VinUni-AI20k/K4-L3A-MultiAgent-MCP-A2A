from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


def _parse_iso(val: Any) -> datetime | None:
    """Parse ISO timestamp string or return None if invalid/missing."""
    if not val or not isinstance(val, str):
        return None
    try:
        # Standardize ISO 8601 timestamps
        cleaned = val.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except Exception:
        return None


@dataclass
class CauseRank:
    cause_code: str
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {"cause_code": self.cause_code, "rank": self.rank}


@dataclass
class ResponsibleParty:
    party_type: str
    party_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"party_type": self.party_type, "party_id": self.party_id}


@dataclass
class RootCauseAnalysis:
    ranked_causes: list[dict[str, Any]] = field(default_factory=list)
    responsible_parties: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ranked_causes": self.ranked_causes,
            "responsible_parties": self.responsible_parties,
        }


@dataclass
class ShipmentInvestigationResult:
    shipment_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    root_cause_analysis: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    primary_issue_candidate: str | None = None
    sla_breached_by: str | None = None  # "seller", "logistics", or None
    delay_days_seller: float = 0.0
    delay_days_logistics: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ShipmentAgent:
    """Specialist Agent for investigating Shipment logistics, SLA compliance,

    and Root Cause Analysis (Member 3).
    """

    ACTOR_NAME: str = "shipment-agent"

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(
        self,
        case_id: str,
        claimed_order_id: str | None,
        known_seller_ids: list[str] | None = None,
        case_opened_at: str | None = None,
    ) -> ShipmentInvestigationResult:
        """Execute shipment and seller investigation for the given case.

        1. Queries get_shipment_summary for tracking timeline and SLA metrics.
        2. Queries get_sellers for seller profile and fulfillment SLA.
        3. Compares timestamps to accurately distinguish between seller delay
           and logistics carrier delay.
        4. Constructs schema-compliant root_cause_analysis and emits observable
           trace events.
        """
        evidence_refs: list[str] = []
        shipment_ids_set: set[str] = set()
        seller_ids_set: set[str] = set(str(s) for s in (known_seller_ids or []) if s)
        ranked_causes: list[dict[str, Any]] = []
        responsible_parties: list[dict[str, Any]] = []
        primary_issue: str | None = None
        sla_breached_by: str | None = None
        delay_days_seller: float = 0.0
        delay_days_logistics: float = 0.0

        if not claimed_order_id:
            # No order to investigate
            return ShipmentInvestigationResult(
                shipment_ids=[],
                seller_ids=list(seller_ids_set),
                root_cause_analysis=RootCauseAnalysis().to_dict(),
                evidence_refs=[],
                primary_issue_candidate=None,
            )

        # --- 1. Query MCP get_shipment_summary ---
        shipment_data: dict[str, Any] = {}
        try:
            shipment_response = await self.gateway.call(
                "get_shipment_summary",
                case_id=case_id,
                order_id=claimed_order_id,
            )
            ev_ref = shipment_response.get("evidence_ref")
            if ev_ref and ev_ref not in evidence_refs:
                evidence_refs.append(ev_ref)

            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=self.ACTOR_NAME,
                tool_name="get_shipment_summary",
                evidence_refs=[ev_ref] if ev_ref else None,
            )

            raw_data = shipment_response.get("data")
            if isinstance(raw_data, dict):
                shipment_data = raw_data
            elif isinstance(raw_data, list) and len(raw_data) > 0 and isinstance(raw_data[0], dict):
                shipment_data = raw_data[0]
        except Exception:
            # Record failed tool call attribute via allowed trace event or skip if unavailable
            pass

        # Extract IDs from shipment_data
        raw_shipment_id = (
            shipment_data.get("shipment_id")
            or shipment_data.get("order_item_id")
            or shipment_data.get("package_id")
        )
        if raw_shipment_id:
            shipment_ids_set.add(str(raw_shipment_id))

        raw_seller_id = shipment_data.get("seller_id")
        if raw_seller_id:
            seller_ids_set.add(str(raw_seller_id))
        elif isinstance(shipment_data.get("seller_ids"), list):
            for s in shipment_data["seller_ids"]:
                if s:
                    seller_ids_set.add(str(s))

        # --- 2. Query MCP get_sellers ---
        for s_id in sorted(list(seller_ids_set))[:3]:
            try:
                seller_response = await self.gateway.call(
                    "get_sellers",
                    case_id=case_id,
                    seller_id=s_id,
                )
                s_ev_ref = seller_response.get("evidence_ref")
                if s_ev_ref and s_ev_ref not in evidence_refs:
                    evidence_refs.append(s_ev_ref)

                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.ACTOR_NAME,
                    tool_name="get_sellers",
                    evidence_refs=[s_ev_ref] if s_ev_ref else None,
                )
            except Exception:
                pass

        # --- 3. Evaluate SLA Timestamps ---
        limit_date = _parse_iso(
            shipment_data.get("shipping_limit_date")
            or shipment_data.get("seller_shipping_limit_date")
            or shipment_data.get("limit_date")
        )
        delivered_carrier = _parse_iso(
            shipment_data.get("order_delivered_carrier_date")
            or shipment_data.get("carrier_handover_date")
            or shipment_data.get("delivered_carrier_date")
        )
        estimated_delivery = _parse_iso(
            shipment_data.get("order_estimated_delivery_date")
            or shipment_data.get("estimated_delivery_date")
        )
        delivered_customer = _parse_iso(
            shipment_data.get("order_delivered_customer_date")
            or shipment_data.get("delivered_customer_date")
        )
        opened_at = _parse_iso(case_opened_at)

        primary_seller_id = (
            sorted(list(seller_ids_set))[0] if seller_ids_set else None
        )
        carrier_name = str(
            shipment_data.get("carrier_name")
            or shipment_data.get("logistics_provider")
            or "logistics_provider"
        )

        # Check Seller Handover SLA:
        # If carrier delivery exists and exceeded limit date:
        seller_late = False
        if limit_date and delivered_carrier:
            if delivered_carrier > limit_date:
                seller_late = True
                delay_days_seller = (delivered_carrier - limit_date).total_seconds() / 86400.0
        elif limit_date and not delivered_carrier:
            # If carrier handover is missing and case was opened past limit date
            check_date = opened_at or datetime.now(limit_date.tzinfo)
            if check_date > limit_date:
                seller_late = True
                delay_days_seller = (check_date - limit_date).total_seconds() / 86400.0

        # Check Logistics SLA:
        logistics_late = False
        if estimated_delivery and delivered_customer:
            if delivered_customer > estimated_delivery:
                logistics_late = True
                delay_days_logistics = (
                    delivered_customer - estimated_delivery
                ).total_seconds() / 86400.0
        elif estimated_delivery and not delivered_customer:
            check_date = opened_at or datetime.now(estimated_delivery.tzinfo)
            if check_date > estimated_delivery:
                logistics_late = True
                delay_days_logistics = (
                    check_date - estimated_delivery
                ).total_seconds() / 86400.0

        # --- 4. Synthesize Root Cause & Issue Attribution ---
        if seller_late:
            primary_issue = "late_delivery_seller"
            sla_breached_by = "seller"
            ranked_causes.append(
                CauseRank(cause_code="SELLER_DISPATCH_TIMEOUT", rank=1).to_dict()
            )
            responsible_parties.append(
                ResponsibleParty(party_type="seller", party_id=primary_seller_id).to_dict()
            )

            # If logistics also took longer, rank as secondary cause
            if logistics_late:
                ranked_causes.append(
                    CauseRank(cause_code="LOGISTICS_DELAY", rank=2).to_dict()
                )
                responsible_parties.append(
                    ResponsibleParty(
                        party_type="logistics_provider", party_id=carrier_name
                    ).to_dict()
                )
        elif logistics_late:
            primary_issue = "late_delivery_logistics"
            sla_breached_by = "logistics"
            ranked_causes.append(
                CauseRank(cause_code="LOGISTICS_DELAY", rank=1).to_dict()
            )
            responsible_parties.append(
                ResponsibleParty(party_type="logistics_provider", party_id=carrier_name).to_dict()
            )
        else:
            # Neither seller nor logistics breached delivery timeline
            sla_breached_by = None

        # Emit handoff event from shipment-agent to coordinator
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.ACTOR_NAME,
            target="coordinator",
            decision_code="SHIPMENT_INVESTIGATED",
            evidence_refs=evidence_refs if evidence_refs else None,
            attributes={
                "sla_breached_by": sla_breached_by,
                "primary_issue": primary_issue,
            },
        )

        return ShipmentInvestigationResult(
            shipment_ids=sorted(list(shipment_ids_set)),
            seller_ids=sorted(list(seller_ids_set)),
            root_cause_analysis=RootCauseAnalysis(
                ranked_causes=ranked_causes,
                responsible_parties=responsible_parties,
            ).to_dict(),
            evidence_refs=evidence_refs,
            primary_issue_candidate=primary_issue,
            sla_breached_by=sla_breached_by,
            delay_days_seller=round(delay_days_seller, 2),
            delay_days_logistics=round(delay_days_logistics, 2),
        )
