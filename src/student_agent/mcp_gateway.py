from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, ToolSpec] | None = None

    async def list_tools(self) -> list[str]:
        return [tool.name for tool in await self.discover_tools()]

    async def get_tools(self) -> list[ToolSpec]:
        if self._tools is None:
            return await self.discover_tools()
        return sorted(self._tools.values(), key=lambda tool: tool.name)

    async def discover_tools(self) -> list[ToolSpec]:
        response = await self._session.list_tools()
        specs = [
            ToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
            )
            for tool in response.tools
        ]
        specs.sort(key=lambda tool: tool.name)
        self._tools = {tool.name: tool for tool in specs}
        return specs

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if self._tools is None:
            raise RuntimeError("MCP tools must be discovered before calling a tool")
        spec = self._tools.get(tool_name)
        if spec is None:
            raise ValueError(f"MCP tool was not discovered: {tool_name}")
        payload = {"case_id": case_id, **arguments}
        schema_errors = sorted(
            Draft202012Validator(spec.input_schema).iter_errors(payload),
            key=lambda error: list(error.absolute_path),
        )
        if schema_errors:
            raise ValueError(
                f"invalid arguments for MCP tool {tool_name}: {schema_errors[0].message}"
            )

        result = None
        for attempt in range(3):
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                is_error = bool(
                    getattr(result, "is_error", getattr(result, "isError", False))
                )
                if is_error:
                    message = " ".join(
                        block.text for block in result.content if getattr(block, "text", None)
                    )
                    raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
                break
            except Exception as exc:
                if attempt == 2 or not _retryable(exc):
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        assert result is not None
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


def _retryable(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in ("timeout", "temporary", "unavailable", "connection", "reset", "rate limit")
    )


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
