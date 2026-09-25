"""Per-case Evidence Ledger and scoped evidence access (ARCHITECTURE.md section 4).

Only ``CaseEvidence.fetch`` registers records, and only from validated MCP responses.
Agents read immutable snapshots; nobody can add or edit a record by hand.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .a2a import ORDER_AGENT, PAYMENT_AGENT, POLICY_AGENT, SHIPMENT_AGENT
from .mcp_gateway import EvidenceGateway, GatewayError

# tool_name -> (allowed actor, expected evidence domains). Verified against discovery
# metadata every run (see ``check_tool_mapping``); unknown tools are never called.
TOOL_MAPPING: dict[str, tuple[str, frozenset[str]]] = {
    "get_order": (ORDER_AGENT, frozenset({"order"})),
    "get_order_items": (ORDER_AGENT, frozenset({"item", "order"})),
    "get_sellers": (ORDER_AGENT, frozenset({"seller"})),
    "get_product_context": (ORDER_AGENT, frozenset({"product", "item"})),
    "get_order_payments": (PAYMENT_AGENT, frozenset({"payment"})),
    "get_payment_timeline": (PAYMENT_AGENT, frozenset({"payment"})),
    "get_refund_timeline": (PAYMENT_AGENT, frozenset({"refund", "payment"})),
    "get_shipment_summary": (SHIPMENT_AGENT, frozenset({"shipment"})),
    "get_policy": (POLICY_AGENT, frozenset({"policy"})),
}


def check_tool_mapping(discovered: dict[str, dict[str, Any]]) -> list[str]:
    """Return mapping problems; an empty list means every mapped tool was discovered."""
    problems = []
    for tool_name in TOOL_MAPPING:
        spec = discovered.get(tool_name)
        if spec is None:
            problems.append(f"{tool_name}: not discovered")
            continue
        required = set(spec.get("inputSchema", {}).get("required", []))
        if "case_id" not in required:
            problems.append(f"{tool_name}: case_id is not a required argument")
    return problems


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Deep, mutable, JSON-shaped copy of a frozen record value."""
    if isinstance(value, MappingProxyType | dict):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class EvidenceRecord:
    local_run_id: str
    case_id: str
    evidence_ref: str
    result_hash: str
    domain: str
    tool_name: str
    actor: str
    request_arguments: Any
    data: Any
    warnings: tuple[str, ...]


class EvidenceLedger:
    def __init__(self, local_run_id: str, case_id: str) -> None:
        self.local_run_id = local_run_id
        self.case_id = case_id
        self._records: dict[str, EvidenceRecord] = {}

    def _register(self, record: EvidenceRecord) -> EvidenceRecord:
        if record.case_id != self.case_id or record.local_run_id != self.local_run_id:
            raise ValueError("CROSS_SCOPE_EVIDENCE_REF")
        existing = self._records.get(record.evidence_ref)
        if existing is not None:
            if (existing.result_hash, existing.domain) != (record.result_hash, record.domain):
                raise ValueError(f"evidence ref collision: {record.evidence_ref}")
            return existing
        self._records[record.evidence_ref] = record
        return record

    def __contains__(self, evidence_ref: object) -> bool:
        return evidence_ref in self._records

    def get(self, evidence_ref: str) -> EvidenceRecord | None:
        return self._records.get(evidence_ref)

    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records.values())

    def domain_of(self, evidence_ref: str) -> str | None:
        record = self._records.get(evidence_ref)
        return record.domain if record else None


class CaseEvidence:
    """Case-scoped gateway view: allowlist, case_id pinning, ledger registration."""

    def __init__(
        self,
        gateway: EvidenceGateway,
        ledger: EvidenceLedger,
        trace: Any,
    ) -> None:
        self._gateway = gateway
        self.ledger = ledger
        self._trace = trace
        self.call_log: list[tuple[str, str, str]] = []  # (actor, tool, outcome)

    @property
    def case_id(self) -> str:
        return self.ledger.case_id

    async def fetch(
        self, actor: str, tool_name: str, *, deadline: float | None = None, **arguments: str
    ) -> EvidenceRecord:
        mapping = TOOL_MAPPING.get(tool_name)
        if mapping is None or mapping[0] != actor:
            raise GatewayError("TOOL_NOT_ALLOWED", f"{actor} may not call {tool_name}")
        if "case_id" in arguments:
            raise GatewayError("INVALID_ARGUMENTS", "case_id is pinned by the case scope")
        try:
            evidence = await self._gateway.call(
                tool_name,
                case_id=self.case_id,
                deadline=deadline or time.monotonic() + 120.0,
                **arguments,
            )
        except GatewayError as exc:
            self.call_log.append((actor, tool_name, exc.code))
            raise
        if evidence["domain"] not in mapping[1]:
            self.call_log.append((actor, tool_name, "UNEXPECTED_DOMAIN"))
            raise GatewayError(
                "INVALID_EVIDENCE_ENVELOPE",
                f"{tool_name} returned domain {evidence['domain']!r}",
            )
        self.call_log.append((actor, tool_name, "ok"))
        record = EvidenceRecord(
            local_run_id=self.ledger.local_run_id,
            case_id=self.case_id,
            evidence_ref=evidence["evidence_ref"],
            result_hash=evidence["result_hash"],
            domain=evidence["domain"],
            tool_name=tool_name,
            actor=actor,
            request_arguments=_freeze(copy.deepcopy(dict(arguments))),
            data=_freeze(copy.deepcopy(evidence["data"])),
            warnings=tuple(evidence.get("warnings", [])),
        )
        return self.ledger._register(record)

    def consume(self, actor: str, records: list[EvidenceRecord]) -> None:
        """Emit tool_result_consumed for evidence actually used, grouped by tool."""
        by_tool: dict[str, list[str]] = {}
        for record in records:
            refs = by_tool.setdefault(record.tool_name, [])
            if record.evidence_ref not in refs:
                refs.append(record.evidence_ref)
        for tool_name in sorted(by_tool):
            refs = by_tool[tool_name]
            for start in range(0, len(refs), 20):
                self._trace.emit(
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    evidence_refs=refs[start : start + 20],
                )
