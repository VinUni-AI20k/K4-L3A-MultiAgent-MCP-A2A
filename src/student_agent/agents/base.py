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

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp.shared.exceptions import MCPError

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

# Lỗi mạng / giao thức tạm thời (timeout, 502 → MCPError) — retry được.
# Lỗi nghiệp vụ (tool trả is_error → RuntimeError) thì KHÔNG retry.
_RETRYABLE = (httpx2.TimeoutException, httpx2.NetworkError, TimeoutError, MCPError)
MAX_TOOL_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5


@dataclass
class EvidenceRecord:
    ref: str
    tool: str
    domain: str
    agent: str
    data: Any = None
    warnings: tuple[str, ...] = ()


@dataclass
class EvidenceLedger:
    """Sổ evidence của MỘT case — chỉ chứa ref thật do MCP trả về cho case đó."""

    case_id: str
    records: dict[str, EvidenceRecord] = field(default_factory=dict)
    # (tool, tham số) đã gọi, kể cả lần lỗi — để coordinator chỉ gọi bù khi cần.
    attempted: set[tuple[str, tuple[tuple[str, str], ...]]] = field(default_factory=set)

    def add(self, evidence: dict[str, Any], tool: str, agent: str) -> None:
        ref = evidence["evidence_ref"]
        self.records.setdefault(
            ref,
            EvidenceRecord(
                ref,
                tool,
                evidence["domain"],
                agent,
                evidence.get("data"),
                tuple(evidence.get("warnings") or ()),
            ),
        )

    def by_tool(self) -> dict[str, EvidenceRecord]:
        """Bản ghi đầu tiên của mỗi tool (thứ tự thu thập)."""
        result: dict[str, EvidenceRecord] = {}
        for record in self.records.values():
            result.setdefault(record.tool, record)
        return result

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


class TransportLost(BaseException):  # noqa: N818
    """Kết nối MCP chết sau khi đã retry. Kế thừa BaseException để các ``except Exception``
    trong agent KHÔNG nuốt mất: case phải được chạy lại trên kết nối mới (cli.py), không
    được ghi ra output thiếu evidence."""


async def call_with_retry(
    gateway: Any, tool_name: str, case_id: str, **arguments: str
) -> dict[str, Any]:
    for attempt in range(1, MAX_TOOL_ATTEMPTS + 1):
        try:
            return await gateway.call(tool_name, case_id=case_id, **arguments)
        except _RETRYABLE as exc:
            if attempt == MAX_TOOL_ATTEMPTS:
                raise TransportLost(f"{tool_name}: {type(exc).__name__}") from exc
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable")


class RecordingGateway:
    """Bọc EvidenceGateway cho agent KHÔNG kế thừa BaseAgent (code của các thành viên khác).

    Mọi evidence trả về cho agent đều được ghi vào ledger của case, nên coordinator /
    verifier biết chính xác ref nào là thật, thuộc domain nào — không cần sửa code agent.
    Agent vẫn tự emit ``tool_result_consumed`` như code gốc của họ.
    """

    def __init__(self, inner: Any, ledger: EvidenceLedger, actor: str) -> None:
        self._inner = inner
        self._ledger = ledger
        self.actor = actor

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if case_id != self._ledger.case_id:
            raise ValueError(f"{self.actor}: case_id {case_id} does not match ledger")
        self._ledger.attempted.add((tool_name, tuple(sorted(arguments.items()))))
        evidence = await call_with_retry(self._inner, tool_name, case_id, **arguments)
        self._ledger.add(evidence, tool_name, self.actor)
        return evidence

    async def list_tools(self) -> list[str]:
        return await self._inner.list_tools()


class TraceTap:
    """Bọc TraceWriter: ghi nhận ref nào đã có ``tool_result_consumed`` trong trace."""

    def __init__(self, inner: TraceWriter) -> None:
        self._inner = inner
        self.consumed: set[str] = set()

    def emit(self, **kwargs: Any) -> dict[str, Any]:
        event = self._inner.emit(**kwargs)
        if kwargs.get("event_type") == "tool_result_consumed":
            self.consumed.update(kwargs.get("evidence_refs") or [])
        return event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


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
        if self.ledger is not None:
            self.ledger.attempted.add((tool_name, tuple(sorted(kwargs.items()))))
        evidence = await call_with_retry(self.gateway, tool_name, case_id, **kwargs)
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
