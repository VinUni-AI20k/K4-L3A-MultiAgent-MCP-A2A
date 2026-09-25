from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from .a2a import Actor
from .mcp_gateway import EvidenceGateway
from .state import CaseState, EvidenceRecord, check_tool_request


class EvidenceCollector:
    """Tạo một collector riêng cho mỗi lần solve_case."""

    def __init__(
        self,
        gateway: EvidenceGateway,
        state: CaseState,
    ) -> None:
        self.gateway = gateway
        self.state = state
        self._slots = asyncio.Semaphore(2)

    async def _call_with_retry(
        self,
        tool_name: str,
        arguments: dict[str, str],
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()

        for attempt in range(3):
            remaining = self.state.deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Case deadline exceeded")

            try:
                async with asyncio.timeout(min(30.0, remaining)):
                    return await self.gateway.call(
                        tool_name,
                        case_id=self.state.case_id,
                        **arguments,
                    )
            except TimeoutError:
                # Retry only explicit timeouts, not generic MCP tool errors.
                if attempt == 2:
                    raise

                delay = float(attempt + 1)
                remaining = self.state.deadline - loop.time()
                if remaining <= delay:
                    raise TimeoutError(
                        "Not enough case time for retry"
                    ) from None

                await asyncio.sleep(delay)

        raise RuntimeError("Unexpected retry state")

    async def collect(
        self,
        actor: Actor,
        tool_name: str,
        **arguments: str,
    ) -> dict[str, Any]:
        check_tool_request(
            self.state, actor, tool_name, arguments
        )

        loop = asyncio.get_running_loop()
        remaining = self.state.deadline - loop.time()

        if remaining <= 0:
            raise TimeoutError("Case deadline exceeded")

        # Thời gian chờ slot cũng nằm trong deadline của case.
        async with asyncio.timeout(remaining):
            async with self._slots:
                remaining = self.state.deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Case deadline exceeded")

                envelope = await self._call_with_retry(
                    tool_name, arguments
                )

                # Gateway đã validate envelope trước khi trả về.
                evidence_ref = envelope["evidence_ref"]
                existing = self.state.evidence.get(evidence_ref)

                if existing is not None:
                    if existing.envelope != envelope:
                        raise ValueError(
                            "Same evidence_ref has conflicting content"
                        )
                else:
                    self.state.evidence[evidence_ref] = EvidenceRecord(
                        case_id=self.state.case_id,
                        actor=actor,
                        tool_name=tool_name,
                        arguments=dict(arguments),
                        envelope=deepcopy(envelope),
                    )

                return deepcopy(envelope)
