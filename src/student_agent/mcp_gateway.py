from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts

CASE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")

# Keep authorization at the gateway boundary. The mapping is deliberately narrow and
# can be replaced after MCP tool discovery if the competition publishes other names.
DEFAULT_TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "order-agent": frozenset({"get_order"}),
    "order-item-agent": frozenset({"get_order", "get_order_items", "get_seller"}),
    "payment-agent": frozenset({"get_order_payments", "get_refunds"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "policy-agent": frozenset({"get_policy"}),
}


class MCPGatewayError(RuntimeError):
    """Base class for normalized MCP gateway failures."""


class MCPPermissionError(MCPGatewayError):
    """An agent attempted to call a tool outside its allowlist."""


class MCPToolError(MCPGatewayError):
    """The MCP server returned a business/tool error."""


class MCPTimeoutError(MCPGatewayError):
    """The MCP call timed out after the bounded retry."""


class MCPResponseError(ValueError):
    """The MCP server returned a malformed evidence response."""


class EvidenceGateway:
    def __init__(
        self,
        session: ClientSession,
        contracts: Contracts,
        *,
        tool_permissions: dict[str, frozenset[str]] | None = None,
        timeout_retries: int = 1,
    ) -> None:
        if timeout_retries not in (0, 1):
            raise ValueError("timeout_retries must be 0 or 1")
        self._session = session
        self._contracts = contracts
        self._tool_permissions = (
            DEFAULT_TOOL_PERMISSIONS if tool_permissions is None else tool_permissions
        )
        self._timeout_retries = timeout_retries

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(
        self,
        tool_name: str,
        *,
        case_id: str,
        actor: str | None = None,
        **arguments: Any,
    ) -> dict[str, Any]:
        """Call a read-only evidence tool and validate its public response envelope.

        ``actor`` is optional temporarily for compatibility with the starter workflow.
        New agent code should always provide it so tool permissions are enforced.
        Only transport timeouts are retried, at most once; tool errors and invalid
        responses are deterministic and are returned immediately as normalized errors.
        """
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise ValueError("case_id does not match the public contract")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name must be a non-empty string")
        if actor is not None:
            allowed_tools = self._tool_permissions.get(actor)
            if allowed_tools is None or tool_name not in allowed_tools:
                raise MCPPermissionError(
                    f"actor {actor!r} is not allowed to call MCP tool {tool_name!r}"
                )

        payload = {"case_id": case_id, **arguments}
        result = await self._call_with_timeout_retry(tool_name, payload)
        if result.isError:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise MCPToolError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise MCPResponseError(
                    f"MCP tool {tool_name} did not return one evidence object"
                )
            try:
                evidence = json.loads(text_blocks[0])
            except (TypeError, json.JSONDecodeError) as exc:
                raise MCPResponseError(
                    f"MCP tool {tool_name} returned invalid evidence JSON"
                ) from exc
        try:
            self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        except ValueError as exc:
            raise MCPResponseError(str(exc)) from exc

        # Return the validated server object unchanged. In particular, never generate,
        # rewrite or normalize evidence_ref/result_hash locally.
        return evidence

    async def _call_with_timeout_retry(
        self, tool_name: str, payload: dict[str, Any]
    ) -> Any:
        for attempt in range(self._timeout_retries + 1):
            try:
                return await self._session.call_tool(tool_name, arguments=payload)
            except (TimeoutError, httpx2.TimeoutException) as exc:
                if attempt == self._timeout_retries:
                    raise MCPTimeoutError(
                        f"MCP tool {tool_name} timed out after {attempt + 1} attempt(s)"
                    ) from exc
        raise AssertionError("unreachable")


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
