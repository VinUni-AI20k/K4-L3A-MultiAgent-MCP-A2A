from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class Evidence:
    """One MCP evidence response, bound to the case and the agent that consumed it."""

    case_id: str
    tool_name: str
    evidence_ref: str
    domain: str
    data: Any
    actor: str
    warnings: tuple[str, ...] = ()


class EvidenceLedger:
    """Run-wide registry of evidence refs returned by the MCP gateway.

    Only refs recorded here may be cited. A ref recorded for one case can never be
    used by another case, which protects the provenance hard gate.
    """

    def __init__(self) -> None:
        self._by_ref: dict[str, Evidence] = {}
        self._by_case: dict[str, list[Evidence]] = {}

    def record(
        self, *, case_id: str, tool_name: str, actor: str, response: dict[str, Any]
    ) -> Evidence:
        ref = response.get("evidence_ref")
        if not isinstance(ref, str) or not EVIDENCE_REF_PATTERN.fullmatch(ref):
            raise EvidenceError(f"{case_id}: {tool_name} returned an invalid evidence_ref")
        existing = self._by_ref.get(ref)
        if existing is not None:
            if existing.case_id != case_id:
                raise EvidenceError(
                    f"{case_id}: evidence {ref} already belongs to {existing.case_id}"
                )
            return existing
        evidence = Evidence(
            case_id=case_id,
            tool_name=tool_name,
            evidence_ref=ref,
            domain=str(response.get("domain", "")),
            data=response.get("data"),
            actor=actor,
            warnings=tuple(response.get("warnings") or ()),
        )
        self._by_ref[ref] = evidence
        self._by_case.setdefault(case_id, []).append(evidence)
        return evidence

    def owns(self, case_id: str, ref: str) -> bool:
        evidence = self._by_ref.get(ref)
        return evidence is not None and evidence.case_id == case_id

    def for_case(self, case_id: str) -> list[Evidence]:
        return list(self._by_case.get(case_id, ()))


async def collect_evidence(
    gateway: EvidenceGateway,
    ledger: EvidenceLedger,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    **arguments: str,
) -> Evidence | None:
    """Call one MCP tool for a specialist, record its evidence and trace the consumption.

    Returns None when the tool reports an error (for example no refund events exist),
    so the caller can continue without inventing data.
    """
    try:
        response = await gateway.call(tool_name, case_id=case_id, **arguments)
    except RuntimeError as exc:
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            decision_code="TOOL_ERROR",
            attributes={"error": str(exc)[:200]},
        )
        return None
    evidence = ledger.record(case_id=case_id, tool_name=tool_name, actor=actor, response=response)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence.evidence_ref],
    )
    return evidence
