from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts

if TYPE_CHECKING:
    from .evidence import ToolDescriptor


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts

    @property
    def contracts(self) -> Contracts:
        return self._contracts

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def describe_tools(self) -> list[ToolDescriptor]:
        """List MCP tools with enough schema detail to call them without guessing names."""
        from .evidence import ToolDescriptor, infer_domain

        response = await self._session.list_tools()
        descriptors = []
        for tool in response.tools:
            schema = tool.input_schema or {}
            properties = tuple(schema.get("properties", {}).keys())
            required = tuple(schema.get("required", ()))
            descriptors.append(
                ToolDescriptor(
                    name=tool.name,
                    domain=infer_domain(tool.name),
                    required_params=required,
                    properties=properties,
                )
            )
        return sorted(descriptors, key=lambda item: item.name)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        # Gateway rule 1 (ARCHITECTURE.md / Pha 3): every MCP call must carry
        # the correct case_id or the server returns 403 Forbidden. Fail fast
        # client-side instead of spending a round-trip on a call that cannot
        # possibly succeed.
        if not case_id:
            raise ValueError(f"MCP tool {tool_name}: case_id is required for every call")
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
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
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
