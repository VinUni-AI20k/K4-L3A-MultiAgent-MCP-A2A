from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from .contracts import ContractError, Contracts

RETRYABLE_TRANSPORT_ERRORS = (httpx2.TransportError, TimeoutError, ConnectionError)
GATEWAY_MAX_ATTEMPTS = 3
GATEWAY_RETRY_BACKOFF_SECONDS = 0.2
MCP_REQUEST_TIMEOUT_SECONDS = 300.0


class MCPTransportError(RuntimeError):
    pass


SessionContext = AbstractAsyncContextManager[ClientSession]
SessionFactory = Callable[[], SessionContext]


def _is_retryable_gateway_error(error: BaseException) -> bool:
    if isinstance(error, MCPError):
        return error.code in {CONNECTION_CLOSED, REQUEST_TIMEOUT}
    if isinstance(error, RETRYABLE_TRANSPORT_ERRORS):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_is_retryable_gateway_error(child) for child in error.exceptions)
    return False


def _contains_mcp_error(error: BaseException) -> bool:
    if isinstance(error, MCPError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_mcp_error(child) for child in error.exceptions)
    return False


def _requires_new_session(error: BaseException) -> bool:
    if isinstance(error, MCPError):
        return error.code == CONNECTION_CLOSED
    if isinstance(error, BaseExceptionGroup):
        return any(_requires_new_session(child) for child in error.exceptions)
    return True


def _is_connection_closed(error: BaseException) -> bool:
    if isinstance(error, MCPError):
        return error.code == CONNECTION_CLOSED
    if isinstance(error, BaseExceptionGroup):
        return any(_is_connection_closed(child) for child in error.exceptions)
    return False


class EvidenceGateway:
    def __init__(
        self,
        session: ClientSession | None,
        contracts: Contracts,
        *,
        session_factory: SessionFactory | None = None,
        max_attempts: int = GATEWAY_MAX_ATTEMPTS,
        retry_backoff_seconds: float = GATEWAY_RETRY_BACKOFF_SECONDS,
        request_timeout_seconds: float = MCP_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._session = session
        self._session_context: SessionContext | None = None
        self._retired_session_contexts: list[SessionContext] = []
        self._session_factory = session_factory
        self._session_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._contracts = contracts
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self._tool_names: list[str] | None = None

    async def start(self) -> None:
        await self._get_session()

    async def close(self) -> None:
        session_context = self._session_context
        retired_contexts = list(self._retired_session_contexts)
        self._session_context = None
        self._retired_session_contexts = []
        self._session = None
        for context in [*retired_contexts, *([session_context] if session_context else [])]:
            with suppress(BaseException):
                await context.__aexit__(None, None, None)

    async def list_tools(self) -> list[str]:
        if self._tool_names is not None:
            return list(self._tool_names)
        async with self._request_lock:
            for attempt in range(1, self.max_attempts + 1):
                session = await self._get_session()
                try:
                    response = await session.list_tools()
                except asyncio.CancelledError as exc:
                    if self._session_factory is None:
                        raise
                    raise MCPTransportError("MCP tool discovery was cancelled") from exc
                except BaseException as exc:
                    if not _is_retryable_gateway_error(exc) or attempt == self.max_attempts:
                        raise
                    await self._prepare_retry(exc, session, attempt)
                else:
                    self._tool_names = sorted(tool.name for tool in response.tools)
                    return list(self._tool_names)
        raise AssertionError("MCP tool discovery retry loop exited unexpectedly")

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        async with self._request_lock:
            for attempt in range(1, self.max_attempts + 1):
                session = await self._get_session()
                try:
                    result = await session.call_tool(
                        tool_name,
                        arguments=payload,
                        read_timeout_seconds=self.request_timeout_seconds,
                    )
                except asyncio.CancelledError as exc:
                    if self._session_factory is None:
                        raise
                    raise MCPTransportError(f"MCP tool {tool_name} was cancelled") from exc
                except BaseException as exc:
                    if not _is_retryable_gateway_error(exc) or attempt == self.max_attempts:
                        raise
                    await self._prepare_retry(exc, session, attempt)
                else:
                    return self._validated_result(tool_name, result)
        raise AssertionError("MCP call retry loop exited unexpectedly")

    async def _get_session(self) -> ClientSession:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("MCP session is not connected")
        async with self._session_lock:
            if self._session is None:
                self._session_context, self._session = await self._enter_session_with_retry()
        if self._session is None:
            raise AssertionError("MCP session factory returned no session")
        return self._session

    async def _enter_session_with_retry(self) -> tuple[SessionContext, ClientSession]:
        if self._session_factory is None:
            raise RuntimeError("MCP session factory is not configured")
        for attempt in range(1, self.max_attempts + 1):
            candidate = self._session_factory()
            try:
                session = await candidate.__aenter__()
            except BaseException as exc:
                if not _is_retryable_gateway_error(exc) and not _contains_mcp_error(exc):
                    raise
                if attempt == self.max_attempts:
                    raise MCPTransportError("MCP session initialization failed") from exc
                await self._backoff(attempt)
            else:
                return candidate, session
        raise AssertionError("MCP session retry loop exited unexpectedly")

    async def _prepare_retry(
        self, error: BaseException, failed_session: ClientSession, attempt: int
    ) -> None:
        await self._backoff(attempt)
        reconnected = await self._replace_failed_session(failed_session)
        if (
            not reconnected
            and _requires_new_session(error)
            and (self._session_factory is not None or _is_connection_closed(error))
        ):
            raise error

    async def _replace_failed_session(self, failed_session: ClientSession) -> bool:
        if self._session_factory is None:
            return False
        async with self._session_lock:
            if self._session is not failed_session:
                return True
            old_context = self._session_context
            self._session_context = None
            self._session = None
            if old_context is not None:
                self._retired_session_contexts.append(old_context)
            self._session_context, self._session = await self._enter_session_with_retry()
            self._tool_names = None
            return True

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))

    def _validated_result(self, tool_name: str, result: Any) -> dict[str, Any]:
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
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
        label = f"MCP tool {tool_name}"
        self._contracts.validate_evidence(evidence, label)
        canonical_data = json.dumps(
            evidence["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical_data.encode("utf-8")).hexdigest()
        if evidence["result_hash"] != f"sha256:{digest}":
            raise ContractError(f"{label}: result_hash does not match canonical data")
        return evidence


@asynccontextmanager
async def _connected_session(endpoint: str, team_api_key: str) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(
        MCP_REQUEST_TIMEOUT_SECONDS, connect=30.0, write=30.0, pool=30.0
    )
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(
            endpoint, http_client=http_client, terminate_on_close=False
        ) as (read_stream, write_stream),
        ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=MCP_REQUEST_TIMEOUT_SECONDS,
        ) as session,
    ):
        await session.initialize()
        yield session


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    gateway = EvidenceGateway(
        None,
        contracts,
        session_factory=lambda: _connected_session(endpoint, team_api_key),
    )
    await gateway.start()
    try:
        yield gateway
    finally:
        await gateway.close()
