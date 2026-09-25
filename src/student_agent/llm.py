from __future__ import annotations

import json
from typing import Any

import httpx2


class OpenRouterClient:
    """Small OpenAI-compatible client with strict JSON response handling."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def complete_json(
        self, *, system: str, user: dict[str, Any], max_tokens: int = 2200
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://day09.vinaction.local",
            "X-Title": "Day09 L3A Student Agent",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "temperature": 0.1,
            "seed": 9,
            "max_tokens": max_tokens,
            "reasoning": {"enabled": False},
            "response_format": {"type": "json_object"},
        }
        timeout = httpx2.Timeout(180.0, connect=30.0, write=30.0, pool=30.0)
        async with httpx2.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
        response.raise_for_status()
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
            value = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("OpenRouter did not return a valid JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("OpenRouter JSON response must be an object")
        return value
