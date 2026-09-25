from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
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
        return evidence


def is_retryable_error(error: BaseException) -> bool:
    """Retry transport-like failures, but never retry malformed requests."""
    if isinstance(error, BaseExceptionGroup):
        return any(is_retryable_error(child) for child in error.exceptions)
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return False
    if isinstance(error, (TimeoutError, OSError)):
        return True
    message = f"{type(error).__name__} {error}".lower()
    markers = (
        "connecterror",
        "readerror",
        "remoteprotocolerror",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "connection reset",
        "connection refused",
        "502",
        "503",
        "504",
        "rate limit",
        "too many requests",
    )
    return any(marker in message for marker in markers)


async def call_with_retry(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    attempts: int = 3,
    backoff_seconds: float = 0.15,
    **arguments: str,
) -> dict[str, Any]:
    """Call an idempotent evidence tool with bounded exponential backoff."""
    if attempts < 1:
        raise ValueError("attempts must be positive")

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await gateway.call(tool_name, case_id=case_id, **arguments)
        except Exception as error:
            last_error = error
            if attempt == attempts - 1 or not is_retryable_error(error):
                raise
            await asyncio.sleep(backoff_seconds * (2**attempt))

    raise RuntimeError(f"MCP tool {tool_name} failed") from last_error


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
