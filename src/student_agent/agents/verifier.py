from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .analysis import build_output


class VerifierAgent:
    """Cross-check specialist evidence and emit the contract-shaped result."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        output = build_output(context)
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code=output["assessment"]["primary_issue"],
            evidence_refs=output["evidence_refs"][:20],
            attributes={
                "confidence": output["assessment"]["confidence"],
                "case_status": output["assessment"]["case_status"],
            },
        )
        return output
