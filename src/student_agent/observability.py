from __future__ import annotations

from copy import deepcopy

from .a2a import Actor, AgentMessage
from .state import CaseState
from .trace import TraceWriter


def record_evidence_consumed(
    state: CaseState,
    trace: TraceWriter,
    actor: Actor,
    evidence_refs: list[str],
) -> None:
    """Ghi nhận agent đã sử dụng evidence có trong state của case."""

    if not evidence_refs:
        raise ValueError("No evidence refs supplied")

    # Bỏ ref lặp, giữ thứ tự.
    refs = list(dict.fromkeys(evidence_refs))
    grouped: dict[str, list[str]] = {}

    # Kiểm tra toàn bộ trước khi bắt đầu ghi trace.
    for ref in refs:
        record = state.evidence.get(ref)
        if record is None:
            raise ValueError("Evidence ref not found in case state")

        if record.case_id != state.case_id:
            raise ValueError("Evidence belongs to another case")

        if record.envelope.get("evidence_ref") != ref:
            raise ValueError("Evidence ref does not match envelope")

        grouped.setdefault(record.tool_name, []).append(ref)

    for tool_name, tool_refs in grouped.items():
        # Trace schema cho phép tối đa 20 refs mỗi event.
        for start in range(0, len(tool_refs), 20):
            trace.emit(
                case_id=state.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=tool_refs[start:start + 20],
            )


def record_message(
    state: CaseState,
    trace: TraceWriter,
    message: AgentMessage,
) -> None:
    """Kiểm tra và ghi nhận message trước khi bên nhận xử lý."""

    actors = {
        "coordinator",
        "order-item-agent",
        "payment-agent",
        "shipment-agent",
        "policy-agent",
        "verifier",
    }

    if message.case_id != state.case_id:
        raise ValueError("Message belongs to another case")

    if message.sender not in actors or message.recipient not in actors:
        raise ValueError("Unknown actor")

    if message.sender == message.recipient:
        raise ValueError("Sender and recipient must differ")

    if message.status not in {
        "pending", "completed", "needs_more_evidence"
    }:
        raise ValueError("Invalid message status")

    if not message.task.strip():
        raise ValueError("Task must not be empty")

    for entity_type, ids in message.entity_scope.items():
        allowed = state.entity_scope.get(entity_type)
        if allowed is None or not set(ids).issubset(allowed):
            raise ValueError("Message entity scope exceeds case scope")

    refs = set(message.evidence_refs)

    for fact in message.facts:
        if not fact.evidence_refs:
            raise ValueError("Fact has no supporting evidence")
        if not set(fact.evidence_refs).issubset(refs):
            raise ValueError("Fact refs missing from message evidence_refs")

    for ref in refs:
        record = state.evidence.get(ref)
        if record is None or record.case_id != state.case_id:
            raise ValueError("Message evidence is outside case state")
        if record.envelope.get("evidence_ref") != ref:
            raise ValueError("Evidence ref does not match envelope")

    snapshot = deepcopy(message)

    event_type = (
        "task_assigned"
        if message.sender == "coordinator"
        and message.status == "pending"
        else "handoff"
    )

    trace.emit(
        case_id=state.case_id,
        event_type=event_type,
        actor=message.sender,
        target=message.recipient,
        attributes={"status": message.status},
    )
    state.messages.append(snapshot)
