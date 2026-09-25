from __future__ import annotations

import os
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel

ProviderType = Literal["ollama", "openai", "gemini"]


def get_llm(
    provider: ProviderType | None = None,
    model_name: str | None = None,
    temperature: float = 0.0,
    **kwargs,
) -> BaseChatModel:
    """Factory function to instantiate chat models for Ollama, OpenAI, or Gemini.

    Configuration can be passed directly or picked up from environment variables:
      - LLM_PROVIDER: "ollama", "openai", or "gemini" (default: "ollama" if no API key set)
      - LLM_MODEL: Model name override
      - OPENAI_API_KEY: For OpenAI
      - GEMINI_API_KEY: For Google Gemini
      - OLLAMA_BASE_URL: For Ollama (default: "http://localhost:11434")
    """
    if provider is None:
        env_prov = os.getenv("LLM_PROVIDER", "").lower()
        if env_prov in ("ollama", "openai", "gemini"):
            provider = env_prov  # type: ignore[assignment]
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            provider = "gemini"
        else:
            provider = "ollama"

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        chosen_model = model_name or os.getenv("LLM_MODEL", "gpt-4o-mini")
        api_key = os.getenv("OPENAI_API_KEY")
        return ChatOpenAI(
            model=chosen_model,
            temperature=temperature,
            api_key=api_key,
            **kwargs,
        )

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        chosen_model = model_name or os.getenv("LLM_MODEL", "gemini-2.0-flash")
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        return ChatGoogleGenerativeAI(
            model=chosen_model,
            temperature=temperature,
            google_api_key=api_key,
            **kwargs,
        )

    elif provider == "ollama":
        from langchain_ollama import ChatOllama

        chosen_model = model_name or os.getenv("LLM_MODEL", "qwen2.5:latest")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        return ChatOllama(
            model=chosen_model,
            temperature=temperature,
            base_url=base_url,
            **kwargs,
        )

    raise ValueError(f"Unsupported LLM provider: {provider}. Expected 'ollama', 'openai', or 'gemini'.")
