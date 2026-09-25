from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx2

DEFAULT_MODEL = "qwen/qwen3-8b"
ALLOWED_MODELS = {
    "qwen/qwen3-8b": 8.2,
    "qwen/qwen3-8b:free": 8.2,
}


class ModelError(RuntimeError):
    """Raised when the bounded OpenRouter verifier cannot return valid JSON."""


@dataclass(frozen=True)
class OpenRouterClient:
    api_key: str
    model: str = DEFAULT_MODEL
    endpoint: str = "https://openrouter.ai/api/v1/chat/completions"

    @classmethod
    def from_env(cls) -> OpenRouterClient:
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required")
        if model not in ALLOWED_MODELS:
            allowed = ", ".join(sorted(ALLOWED_MODELS))
            raise ValueError(f"OPENROUTER_MODEL must be a verified sub-10B model: {allowed}")
        return cls(api_key=api_key, model=model)

    async def verify_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        system = (
            "You are the verifier in an ecommerce complaint investigation. "
            "Use only the supplied authoritative MCP evidence. Never invent identifiers, "
            "money, facts, or evidence references. Return JSON only with keys "
            "primary_issue, confidence, and flags. primary_issue must be one supplied "
            "candidate; confidence must be 0..1; flags must be short machine codes."
        )
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
            "reasoning": {"enabled": False, "exclude": True},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": "Day09 L3A Student Agent",
        }
        timeout = httpx2.Timeout(90.0, connect=30.0, write=30.0, pool=30.0)
        try:
            async with httpx2.AsyncClient(timeout=timeout) as client:
                response = await client.post(self.endpoint, headers=headers, json=request)
                response.raise_for_status()
                body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ModelError("OpenRouter returned non-text content")
            value = json.loads(_strip_code_fence(content))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelError("OpenRouter returned an invalid JSON response") from exc
        except httpx2.HTTPError as exc:
            raise ModelError("OpenRouter request failed") from exc
        if not isinstance(value, dict):
            raise ModelError("OpenRouter verifier response must be an object")
        return value


def _strip_code_fence(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped
