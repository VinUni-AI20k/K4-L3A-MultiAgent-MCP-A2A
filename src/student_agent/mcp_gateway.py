from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class SessionLostError(RuntimeError):
    """The MCP session broke; evidence from a new session would belong to another run."""


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self.session_error: str | None = None
        self.tool_successes = 0
        self.tool_errors: Counter[str] = Counter()

    @property
    def session_lost(self) -> bool:
        return self.session_error is not None

    async def _request(self, operation: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        """Run one MCP request. Protocol/session failures mark the whole session as lost.

        Tool-level errors are not exceptions here (they come back as is_error results).
        After a session failure every call fails fast, so the caller can restart the run
        in a fresh session instead of mixing evidence from two sessions.
        """
        if self.session_error is not None:
            raise SessionLostError(f"MCP session lost: {self.session_error}")
        try:
            return await operation(self._session)
        except Exception as exc:
            self.session_error = f"{type(exc).__name__}: {exc}"[:200]
            raise SessionLostError(f"MCP session lost: {self.session_error}") from exc

    async def list_tools(self) -> list[str]:
        response = await self._request(lambda session: session.list_tools())
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = await self._request(
            lambda session: session.call_tool(tool_name, arguments=payload)
        )
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error:
            self.tool_errors[tool_name] += 1
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        self.tool_successes += 1
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
