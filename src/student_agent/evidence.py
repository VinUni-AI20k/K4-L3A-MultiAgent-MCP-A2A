"""Case-local evidence ledger and observable specialist handoffs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx2

from .mcp_gateway import EvidenceGateway, ToolFailure
from .trace import TraceWriter

DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
}


@dataclass
class Finding:
    actor: str
    facts: dict[str, Any]
    tools: list[str]


@dataclass
class CaseEvidence:
    case_id: str
    order_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    catalog: set[str]
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    attempts: int = 2
    timeout: float = 45.0

    def assign(self, actor: str, code: str) -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=code,
        )

    def handoff(self, finding: Finding, target: str = "coordinator") -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=finding.actor,
            target=target,
            decision_code="FINDINGS_READY",
            evidence_refs=self.refs(finding.tools),
        )

    def refs(self, names: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                self.records[name]["evidence_ref"] for name in names if name in self.records
            )
        )

    def data(self, name: str) -> Any:
        return self.records[name]["data"] if name in self.records else None

    def _validate_scope(self, tool: str, evidence: dict[str, Any]) -> None:
        self.trace.contracts.validate_evidence(evidence)
        if evidence["domain"] != DOMAINS[tool]:
            raise ValueError(f"{self.case_id}: wrong evidence domain for {tool}")

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if "case_id" in value and value["case_id"] != self.case_id:
                    raise ValueError(f"{self.case_id}: cross-case evidence rejected")
                if "order_id" in value and value["order_id"] != self.order_id:
                    raise ValueError(f"{self.case_id}: cross-order evidence rejected")
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(evidence["data"])

    async def fetch(
        self, actor: str, tool: str, *, required: bool = False, **arguments: str
    ) -> Any:
        if tool in self.records:
            return self.data(tool)
        if tool not in self.catalog:
            self.failures[tool] = "TOOL_NOT_DISCOVERED"
            if required:
                raise RuntimeError(f"Required MCP tool not discovered: {tool}")
            return None
        for attempt in range(self.attempts):
            try:
                response = await asyncio.wait_for(
                    self.gateway.call(tool, case_id=self.case_id, **arguments), self.timeout
                )
                self._validate_scope(tool, response)
                self.records[tool] = response
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool,
                    evidence_refs=[response["evidence_ref"]],
                )
                return response["data"]
            except (ToolFailure, TimeoutError, httpx2.TransportError):
                if attempt + 1 < self.attempts:
                    await asyncio.sleep(0.25)
        self.failures[tool] = "MCP_UNAVAILABLE"
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            tool_name=tool,
            decision_code="MCP_UNAVAILABLE",
            attributes={"attempts": self.attempts},
        )
        if required:
            raise RuntimeError(
                f"{self.case_id}: required MCP evidence unavailable: {tool}; "
                "no submission generated from server errors"
            )
        return None
