from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from jsonschema import Draft202012Validator
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class ToolFailure(RuntimeError):
    """Server error, not evidence that a business entity does not exist."""


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, dict[str, Any]] | None = None
        self.evidence_calls: dict[str, tuple[str, str]] = {}

    async def list_tools(self) -> list[str]:
        if self._tools is None:
            catalog: dict[str, dict[str, Any]] = {}
            cursor = None
            seen_cursors: set[str] = set()
            while True:
                params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
                response = await self._session.list_tools(params=params)
                for tool in response.tools:
                    schema = getattr(tool, "input_schema", None)
                    if schema is None:
                        schema = getattr(tool, "inputSchema", None)
                    if not isinstance(schema, dict):
                        raise ValueError(f"MCP tool {tool.name} has no input schema")
                    catalog[tool.name] = schema
                cursor = getattr(response, "next_cursor", None)
                if not cursor:
                    break
                if cursor in seen_cursors:
                    raise ValueError("MCP discovery returned a repeated pagination cursor")
                seen_cursors.add(cursor)
            self._tools = catalog
        return sorted(self._tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        await self.list_tools()
        if self._tools is None or tool_name not in self._tools:
            raise ValueError(f"Tool not in discovered MCP catalog: {tool_name}")
        Draft202012Validator(self._tools[tool_name]).validate(payload)
        result = await self._session.call_tool(tool_name, arguments=payload)
        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
            # Do not copy arbitrary server text (which may include credentials)
            # into traces or CLI logs. A failed call is never a not-found record.
            raise ToolFailure(f"MCP tool {tool_name} failed for {case_id}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        ref = evidence["evidence_ref"]
        owner = self.evidence_calls.get(ref)
        if owner is not None and owner != (case_id, tool_name):
            raise ValueError("MCP evidence reference reused across case/tool scope")
        self.evidence_calls[ref] = (case_id, tool_name)
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
