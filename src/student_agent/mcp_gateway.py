from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


def _group_leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _group_leaves(child)]
    return [error]


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tool_names: frozenset[str] | None = None

    async def list_tools(self) -> list[str]:
        if self._tool_names is None:
            response = await self._session.list_tools()
            self._tool_names = frozenset(tool.name for tool in response.tools)
        return sorted(self._tool_names)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if tool_name not in await self.list_tools():
            raise RuntimeError(f"MCP tool is not available: {tool_name}")
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


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    try:
        async with (
            httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
            streamable_http_client(endpoint, http_client=http_client) as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            yield EvidenceGateway(session, contracts)
    except BaseExceptionGroup as error:
        leaves = _group_leaves(error)
        application_error = next(
            (leaf for leaf in leaves if isinstance(leaf, (RuntimeError, ValueError))), None
        )
        if application_error is not None:
            raise application_error from error
        details = list(dict.fromkeys(str(leaf) for leaf in leaves if str(leaf)))
        message = "; ".join(details) or "unknown connection or session error"
        raise RuntimeError(f"MCP Gateway unavailable: {message}") from error
    except (httpx2.HTTPError, OSError) as error:
        raise RuntimeError(f"MCP Gateway unavailable: {error}") from error
