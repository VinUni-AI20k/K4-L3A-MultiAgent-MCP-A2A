"""MCP Evidence Gateway client.

Speaks the MCP streamable-HTTP transport directly (initialize -> notifications/initialized
-> tools/list|tools/call -> DELETE session) so that every request has an explicit
timeout, bounded retry for transient failures and a clean shutdown. The server audits
every call; this client never invents or edits evidence.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2

from .contracts import ContractError, Contracts

PROTOCOL_VERSION = "2025-06-18"
CALL_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 4  # 1 call + up to 3 retries, transient failures only
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_SECONDS = 30.0
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class GatewayError(RuntimeError):
    """A single call failed; the case may continue with other evidence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class GatewayFatalError(GatewayError):
    """Authentication/configuration failure: the whole run must stop."""


class ToolCallError(GatewayError):
    """The MCP tool answered with isError (e.g. evidence not found). Never retried."""


def _retry_after(response: httpx2.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _parse_body(response: httpx2.Response, request_id: int) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    messages: list[dict[str, Any]] = []
    if "text/event-stream" in content_type:
        data_lines: list[str] = []
        for line in response.text.splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                messages.append(json.loads("\n".join(data_lines)))
                data_lines = []
    else:
        parsed = json.loads(response.text)
        messages.extend(parsed if isinstance(parsed, list) else [parsed])
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise GatewayError("MCP_PROTOCOL_ERROR", "no JSON-RPC response for request")


class McpHttpSession:
    def __init__(self, endpoint: str, team_api_key: str) -> None:
        self._endpoint = endpoint
        self._headers = {
            "Authorization": f"Bearer {team_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        timeout = httpx2.Timeout(CALL_TIMEOUT_SECONDS, connect=20.0)
        self._client = httpx2.AsyncClient(timeout=timeout)
        self._session_id: str | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self.stats = {"requests": 0, "retries": 0}

    def _headers_for_session(self) -> dict[str, str]:
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    async def _post(self, message: dict[str, Any], deadline: float) -> httpx2.Response:
        """POST once per attempt; retry only transient transport/HTTP failures."""
        last_error = "unknown"
        for attempt in range(MAX_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError("TASK_DEADLINE_EXCEEDED", "no time left for MCP call")
            self.stats["requests"] += 1
            wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            try:
                if "id" in message:
                    response = await self._client.post(
                        self._endpoint,
                        json=message,
                        headers=self._headers_for_session(),
                        timeout=min(CALL_TIMEOUT_SECONDS, remaining),
                    )
                else:
                    # Notifications get 202 with a chunked body the server may keep open;
                    # only the status matters, so never wait for that body.
                    async with self._client.stream(
                        "POST",
                        self._endpoint,
                        json=message,
                        headers=self._headers_for_session(),
                        timeout=min(CALL_TIMEOUT_SECONDS, remaining),
                    ) as response:
                        pass
            except httpx2.TransportError as exc:
                last_error = f"MCP_UNAVAILABLE ({type(exc).__name__})"
            else:
                if response.status_code in (401, 403):
                    raise GatewayFatalError(
                        "MCP_AUTH_FAILED", f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                if response.status_code == 404 and self._session_id:
                    raise GatewayError("MCP_SESSION_EXPIRED", "session not found")
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                last_error = (
                    "MCP_RATE_LIMITED" if response.status_code == 429 else "MCP_UNAVAILABLE"
                ) + f" (HTTP {response.status_code})"
                retry_after = _retry_after(response)
                if retry_after is not None:
                    if retry_after > MAX_RETRY_AFTER_SECONDS:
                        break
                    wait = max(wait, retry_after)
            if attempt + 1 >= MAX_ATTEMPTS or time.monotonic() + wait >= deadline:
                break
            self.stats["retries"] += 1
            await asyncio.sleep(wait)
        code = last_error.split(" ", 1)[0]
        raise GatewayError(code, last_error)

    async def open(self) -> None:
        self._session_id = None
        deadline = time.monotonic() + 120.0
        request_id = self._new_id()
        response = await self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "day09-student-agent", "version": "0.1.0"},
                },
            },
            deadline,
        )
        if response.status_code != 200:
            raise GatewayFatalError("MCP_INIT_FAILED", f"HTTP {response.status_code}")
        message = _parse_body(response, request_id)
        if "error" in message:
            raise GatewayFatalError("MCP_INIT_FAILED", json.dumps(message["error"])[:200])
        self._session_id = response.headers.get("mcp-session-id")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, deadline)

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def request(self, method: str, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        async with self._lock:
            for reopen in (False, True):
                if reopen:
                    await self.open()
                request_id = self._new_id()
                try:
                    response = await self._post(
                        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                        deadline,
                    )
                except GatewayError as exc:
                    if exc.code == "MCP_SESSION_EXPIRED" and not reopen:
                        continue
                    raise
                if response.status_code >= 400:
                    raise GatewayError(
                        "MCP_HTTP_ERROR", f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                message = _parse_body(response, request_id)
                if "error" in message:
                    error = message["error"]
                    raise GatewayError("MCP_RPC_ERROR", json.dumps(error)[:300])
                result = message.get("result")
                if not isinstance(result, dict):
                    raise GatewayError("MCP_PROTOCOL_ERROR", "result is not an object")
                return result
        raise GatewayError("MCP_SESSION_EXPIRED", "could not re-open session")

    async def close(self) -> None:
        try:
            if self._session_id:
                await self._client.delete(
                    self._endpoint, headers=self._headers_for_session(), timeout=10.0
                )
        except httpx2.HTTPError:
            pass
        finally:
            await self._client.aclose()


class EvidenceGateway:
    def __init__(self, session: McpHttpSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, dict[str, Any]] = {}

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._session.stats)

    async def discover(self) -> dict[str, dict[str, Any]]:
        tools: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self._session.request("tools/list", params, time.monotonic() + 120.0)
            for tool in result.get("tools", []):
                tools[tool["name"]] = tool
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self._tools = tools
        return tools

    async def list_tools(self) -> list[str]:
        if not self._tools:
            await self.discover()
        return sorted(self._tools)

    def tool_spec(self, tool_name: str) -> dict[str, Any] | None:
        return self._tools.get(tool_name)

    async def call(
        self,
        tool_name: str,
        *,
        case_id: str,
        deadline: float | None = None,
        **arguments: str,
    ) -> dict[str, Any]:
        if self._tools and tool_name not in self._tools:
            raise GatewayError("TOOL_NOT_DISCOVERED", tool_name)
        payload = {"case_id": case_id, **arguments}
        spec = self._tools.get(tool_name)
        if spec:
            required = spec.get("inputSchema", {}).get("required", [])
            missing = [name for name in required if not payload.get(name)]
            if missing:
                raise GatewayError("INVALID_ARGUMENTS", f"{tool_name} missing {missing}")
        result = await self._session.request(
            "tools/call",
            {"name": tool_name, "arguments": payload},
            deadline or time.monotonic() + 120.0,
        )
        if result.get("isError"):
            message = " ".join(
                block.get("text", "") for block in result.get("content", []) if block.get("text")
            )
            raise ToolCallError("EVIDENCE_NOT_FOUND", f"{tool_name}: {message or 'tool error'}")
        evidence = result.get("structuredContent")
        if evidence is None:
            texts = [block.get("text") for block in result.get("content", []) if block.get("text")]
            if len(texts) != 1:
                raise GatewayError(
                    "INVALID_EVIDENCE_ENVELOPE", f"{tool_name} did not return one evidence object"
                )
            try:
                evidence = json.loads(texts[0])
            except json.JSONDecodeError as exc:
                raise GatewayError("INVALID_EVIDENCE_ENVELOPE", str(exc)) from exc
        try:
            self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        except ContractError as exc:
            raise GatewayError("INVALID_EVIDENCE_ENVELOPE", str(exc)) from exc
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    session = McpHttpSession(endpoint, team_api_key)
    try:
        await session.open()
        gateway = EvidenceGateway(session, contracts)
        await gateway.discover()
        yield gateway
    finally:
        await session.close()
