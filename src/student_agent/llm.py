from __future__ import annotations

import json
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from openai import AsyncOpenAI

from .config import Settings


class JSONModel(Protocol):
    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]: ...


class LLMClient:
    """Small OpenAI-compatible adapter with one bounded structured-output repair."""

    def __init__(self, *, base_url: str, api_key: str, timeout: float) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._ollama = "127.0.0.1:11434" in base_url or "localhost:11434" in base_url

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMClient:
        return cls(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
        )

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        validator = Draft202012Validator(schema)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "Return only JSON matching the supplied schema.\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        last_error = "unknown structured-output error"
        for attempt in range(2):
            request: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": 0,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
            }
            if max_tokens is not None:
                request["max_tokens"] = max_tokens
            if self._ollama:
                request["extra_body"] = {
                    "think": False,
                    "keep_alive": "30m",
                    "options": {"num_ctx": 8192},
                }
            response = await self._client.chat.completions.create(**request)
            content = response.choices[0].message.content or ""
            try:
                value = _decode_object(content)
                errors = sorted(
                    validator.iter_errors(value), key=lambda error: list(error.absolute_path)
                )
                if not errors:
                    return value
                first = errors[0]
                location = ".".join(str(part) for part in first.absolute_path) or "$"
                last_error = f"{location}: {first.message}"
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            if attempt == 0:
                messages.extend(
                    [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                f"The response is invalid ({last_error}). Repair it as JSON only."
                            ),
                        },
                    ]
                )
        raise ValueError(f"model {model} returned invalid {schema_name}: {last_error}")


def _decode_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise TypeError("model response must be a JSON object")
    return value
