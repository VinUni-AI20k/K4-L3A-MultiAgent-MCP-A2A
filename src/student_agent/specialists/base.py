from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


@dataclass
class SpecialistResult:
    """Standardized output produced by any specialist investigation."""

    actor: str
    facts: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    entities: dict[str, set[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "reasoning": self.reasoning,
            "facts": self.facts,
            "evidence_refs": self.evidence_refs,
            "entities": {k: sorted(list(v)) for k, v in self.entities.items()},
            "errors": self.errors,
        }


class BaseSpecialist(ABC):
    """Abstract base class for all specialist agents.
    
    Supports both LLM-driven reasoning/tool-calling and deterministic MCP extraction.
    """

    def __init__(self, name: str, llm: BaseChatModel | None = None) -> None:
        self.name = name
        self.llm = llm

    async def safe_call_tool(
        self,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        tool_name: str,
        case_id: str,
        **kwargs: str,
    ) -> dict[str, Any] | None:
        """Call an MCP tool with error isolation and trace emission."""
        try:
            evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
            evidence_ref = evidence.get("evidence_ref")
            if evidence_ref and trace:
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.name,
                    tool_name=tool_name,
                    evidence_refs=[evidence_ref],
                )
            return evidence
        except Exception as exc:
            logger.warning("Tool '%s' call failed for case '%s': %s", tool_name, case_id, exc)
            return None

    @abstractmethod
    async def investigate(
        self,
        case_id: str,
        order_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        customer_context: str = "",
    ) -> SpecialistResult:
        """Execute domain-specific investigation and return a structured SpecialistResult."""
        pass
