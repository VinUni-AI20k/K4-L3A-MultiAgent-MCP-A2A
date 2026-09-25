"""Base agent class — shared by all specialist agents.

Owner: Thành viên 1 (feat/tv1-coordinator-trace)

Contract giữa specialist và coordinator
---------------------------------------
Ngoài các key riêng của domain, mỗi specialist nên trả thêm key ``issues``::

    "issues": [
        {
            "issue": "late_delivery_seller",   # 1 trong PRIMARY_ISSUES
            "strength": 0.9,                   # 0..1, độ chắc chắn dựa trên evidence
            "evidence_refs": ["ev_..."],       # CHỈ các ref chứng minh issue này
            "case_status": "action_required",  # tùy chọn — ghi đè mặc định
        },
    ]

Dùng ``self.signal(...)`` để tạo phần tử cho gọn. Không có vấn đề → ``issues`` rỗng.
Coordinator (adjudicator) sẽ chọn ``primary_issue`` cuối cùng từ các tín hiệu này.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx2

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter

PRIMARY_ISSUES = (
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
)

# Lỗi mạng tạm thời — retry được. Lỗi nghiệp vụ (tool trả isError) thì không retry.
_RETRYABLE = (httpx2.TimeoutException, httpx2.NetworkError, TimeoutError)
MAX_TOOL_ATTEMPTS = 2


@dataclass
class EvidenceRecord:
    ref: str
    tool: str
    domain: str
    agent: str


@dataclass
class EvidenceLedger:
    """Sổ evidence của MỘT case — chỉ chứa ref thật do MCP trả về cho case đó."""

    case_id: str
    records: dict[str, EvidenceRecord] = field(default_factory=dict)

    def add(self, evidence: dict[str, Any], tool: str, agent: str) -> None:
        ref = evidence["evidence_ref"]
        self.records.setdefault(ref, EvidenceRecord(ref, tool, evidence["domain"], agent))

    def __contains__(self, ref: object) -> bool:
        return ref in self.records

    def refs(self, domains: set[str] | None = None) -> list[str]:
        """Ref theo thứ tự thu thập, lọc theo domain nếu có."""
        return [
            ref
            for ref, record in self.records.items()
            if domains is None or record.domain in domains
        ]

    def only_known(self, refs: list[str]) -> list[str]:
        """Bỏ ref không thuộc case này (chống hard gate provenance) và ref trùng."""
        return [ref for ref in dict.fromkeys(refs) if ref in self.records]


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
        # Coordinator gán ledger dùng chung cho cả case; mặc định là ledger riêng.
        self.ledger: EvidenceLedger | None = None

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

        Retries once on transient network errors. Returns the full evidence dict
        (contains ``evidence_ref``, ``domain``, ``data``, ...).
        """
        if self.ledger is not None and self.ledger.case_id != case_id:
            raise ValueError(f"{self.name}: case_id {case_id} does not match ledger")
        for attempt in range(1, MAX_TOOL_ATTEMPTS + 1):
            try:
                evidence = await self.gateway.call(tool_name, case_id=case_id, **kwargs)
                break
            except _RETRYABLE:
                if attempt == MAX_TOOL_ATTEMPTS:
                    raise
        if self.ledger is not None:
            self.ledger.add(evidence, tool_name, self.name)
        self.trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=self.name,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
        )
        return evidence

    # ------------------------------------------------------------------
    # Signal helper — see module docstring
    # ------------------------------------------------------------------

    @staticmethod
    def signal(
        issue: str,
        strength: float,
        evidence_refs: list[str],
        case_status: str | None = None,
    ) -> dict[str, Any]:
        if issue not in PRIMARY_ISSUES:
            raise ValueError(f"unknown primary_issue: {issue}")
        result: dict[str, Any] = {
            "issue": issue,
            "strength": max(0.0, min(1.0, float(strength))),
            "evidence_refs": list(dict.fromkeys(evidence_refs)),
        }
        if case_status is not None:
            result["case_status"] = case_status
        return result

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
