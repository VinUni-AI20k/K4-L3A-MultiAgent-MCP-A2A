"""Internal Agent-to-Agent (A2A) message envelope.

Per ARCHITECTURE.md Sec 4: every handoff between Coordinator, specialists,
Policy Agent and Verifier Agent is carried as an ``AgentMessage``. This is an
internal protocol structure -- it is never serialized into the public output
or submission contract. Only a safe subset of it (actor, target, decision
code, evidence refs, a handful of scalar attributes) is ever projected into
an observable `trace.emit(...)` call.

`status` describes handoff progress, not business conclusions:
``completed`` | ``not_found`` | ``unavailable`` | ``insufficient_evidence``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from .trace import TraceWriter

STATUS_VALUES = ("completed", "not_found", "unavailable", "insufficient_evidence")


def new_task_id() -> str:
    return f"tsk_{secrets.token_urlsafe(12)}"


@dataclass(frozen=True)
class AgentMessage:
    case_id: str
    task_id: str
    from_actor: str
    to_actor: str
    domain: str | None = None
    claim_ids: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    facts: dict[str, Any] | None = None
    evidence_refs: tuple[str, ...] = ()
    status: str = "completed"
    error_code: str | None = None
    attempt: int = 0

    def __post_init__(self) -> None:
        if self.status not in STATUS_VALUES:
            raise ValueError(f"invalid A2A status: {self.status!r}")


def emit(trace: TraceWriter, message: AgentMessage, *, event_type: str) -> None:
    """Project the safe, observable subset of an AgentMessage into the trace.

    Never writes `facts` (raw evidence payload) or `identifiers` -- only
    correlation and outcome metadata, per the "no evidence payload / no
    reasoning content in trace" rule.
    """
    attributes: dict[str, str | int | float | bool | None] = {
        "task_id": message.task_id,
        "status": message.status,
    }
    if message.attempt:
        attributes["attempt"] = message.attempt
    if message.claim_ids:
        attributes["claim_count"] = len(message.claim_ids)
    trace.emit(
        case_id=message.case_id,
        event_type=event_type,
        actor=message.from_actor,
        target=message.to_actor,
        decision_code=message.error_code,
        evidence_refs=list(message.evidence_refs) or None,
        attributes=attributes,
    )
