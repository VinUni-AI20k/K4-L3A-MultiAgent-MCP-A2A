from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Qwen LLM Configuration. Accepts the generic names (API_KEY/BASE_URL/MODEL_NAME)
# or the OpenRouter names used in the team's .env (OPENROUTER_API_KEY/OPENROUTER_MODEL).
DASHSCOPE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


def _settings() -> tuple[str, str, str]:
    api_key = os.getenv("API_KEY") or os.getenv("OPENROUTER_API_KEY") or ""
    default_url = OPENROUTER_URL if os.getenv("OPENROUTER_API_KEY") else DASHSCOPE_URL
    base_url = (os.getenv("BASE_URL") or default_url).rstrip("/")
    model_name = os.getenv("MODEL_NAME") or os.getenv("OPENROUTER_MODEL") or "qwen-3-8b"
    return api_key, base_url, model_name


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    response_format_json: bool = False,
    timeout: float = 60.0,
) -> str:
    """Send a chat completion request to Qwen via OpenAI-compatible endpoint.

    Uses `httpx2` which is already included in the repository dependencies.
    """
    api_key, base_url, default_model = _settings()
    if not api_key or api_key in ("your_qwen_api_key_here", "replace_me"):
        raise ValueError(
            "Chưa cấu hình API_KEY hoặc OPENROUTER_API_KEY trong file .env"
        )
    model_name = model or default_model

    # Đảm bảo tiền tố phù hợp nếu dùng OpenRouter
    if "openrouter.ai" in base_url and "/" not in model_name:
        model_name = f"qwen/{model_name}"

    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format_json:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    import httpx2

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Lỗi gọi Qwen API (HTTP {response.status_code}): {response.text}"
            )
        data = response.json()
        return data["choices"][0]["message"]["content"]
