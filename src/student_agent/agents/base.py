"""Base agent class — shared by all specialist agents.

Owner: Thành viên 1 (feat/tv1-coordinator-trace)
"""

from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


class BaseAgent:
    """Abstract base class for all specialist agents.

    Provides common utilities: MCP tool calling with automatic trace emission,
    and a standard ``run()`` interface that subclasses must implement.
    """

    def __init__(
        self,
        name: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> None:
        self.name = name
        self.gateway = gateway
        self.trace = trace

    # ------------------------------------------------------------------
    # MCP helper — call a tool and emit tool_result_consumed
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        tool_name: str,
        case_id: str,
        **kwargs: str,
    ) -> dict[str, Any]:
        """Call an MCP tool and automatically emit a ``tool_result_consumed`` trace event.

        Returns the full evidence dict (contains ``evidence_ref``, ``data``, etc.).
        """
        evidence = await self.gateway.call(tool_name, case_id=case_id, **kwargs)
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=self.name,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
        )
        return evidence

    # ------------------------------------------------------------------
    # Trace helpers
    # ------------------------------------------------------------------

    def emit_handoff(self, case_id: str, target: str, **attrs: Any) -> None:
        """Emit a ``handoff`` event when passing results to another agent."""
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.name,
            target=target,
            attributes=attrs if attrs else None,
        )

    # ------------------------------------------------------------------
    # Main entry point — subclasses override this
    # ------------------------------------------------------------------

    async def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Execute the agent's task and return its results.

        Args:
            case_id: The case identifier (e.g. ``L3A_CASE_010``).
            context: Shared context dict built up by the coordinator.

        Returns:
            A dict of results specific to this agent's domain.
        """
        raise NotImplementedError(f"{type(self).__name__}.run() not implemented")
