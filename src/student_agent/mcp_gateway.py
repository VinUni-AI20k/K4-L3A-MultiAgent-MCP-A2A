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
        self._tool_names: list[str] | None = None

    async def list_tools(self) -> list[str]:
        if self._tool_names is not None:
            return list(self._tool_names)
        response = await self._session.list_tools()
        self._tool_names = sorted(tool.name for tool in response.tools)
        return list(self._tool_names)

    def validate_output(self, value: dict[str, Any], label: str = "workflow output") -> None:
        """Expose output validation without leaking the contract implementation to agents."""
        self._contracts.validate_output(value, label)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = None
        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0, 0.5, 1.5), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                break
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    raise RuntimeError(
                        f"MCP transport failed for {tool_name} after {attempt} attempts"
                    ) from exc
        if result is None:  # Defensive guard; the loop either returns a result or raises.
            raise RuntimeError(f"MCP transport failed for {tool_name}: {last_error}")
        if result.is_error:
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
    endpoint: str,
    team_api_key: str,
    contracts: Contracts,
    *,
    preflight: bool = True,
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    last_error: Exception | None = None

    for attempt, delay in enumerate((0.0, 1.0, 3.0), start=1):
        if delay:
            await asyncio.sleep(delay)
        yielded = False
        body_completed = False
        try:
            async with (
                httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
                streamable_http_client(
                    endpoint, http_client=http_client
                ) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                gateway = EvidenceGateway(session, contracts)
                if preflight:
                    await gateway.list_tools()
                yielded = True
                yield gateway
                body_completed = True
                return
        except Exception as exc:
            if yielded:
                if body_completed:
                    # The caller completed successfully; a background transport error
                    # while closing this short-lived session must not discard the case.
                    return
                raise
            last_error = exc
            if attempt == 3:
                raise RuntimeError(
                    f"MCP connection failed after {attempt} attempts"
                ) from exc

    raise RuntimeError(f"MCP connection failed: {last_error}")
