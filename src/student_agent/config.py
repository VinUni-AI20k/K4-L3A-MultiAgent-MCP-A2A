from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TEAM_KEY_PATTERN = re.compile(r"^sk-team-[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class Settings:
    competition_api_url: str
    team_api_key: str
    mcp_endpoint: str
    llm_base_url: str
    llm_api_key: str
    coordinator_model: str
    specialist_model: str
    verifier_model: str
    llm_timeout_seconds: float
    root: Path

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env")
        api_url = os.getenv("COMPETITION_API_URL", "").strip().rstrip("/")
        team_key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
        mcp_endpoint = os.getenv("MCP_ENDPOINT", "").strip()
        llm_base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1").strip().rstrip("/")
        llm_api_key = os.getenv("LLM_API_KEY", "ollama").strip()
        coordinator_model = os.getenv("LLM_COORDINATOR_MODEL", "qwen3:0.6b").strip()
        specialist_model = os.getenv("LLM_SPECIALIST_MODEL", "qwen3:1.7b").strip()
        verifier_model = os.getenv("LLM_VERIFIER_MODEL", "qwen3:4b").strip()
        timeout_text = os.getenv("LLM_TIMEOUT_SECONDS", "180").strip()
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if not llm_base_url.startswith(("http://", "https://")):
            errors.append("LLM_BASE_URL must be an absolute HTTP(S) URL")
        if not all((llm_api_key, coordinator_model, specialist_model, verifier_model)):
            errors.append("LLM API key and model names must be non-empty")
        try:
            llm_timeout_seconds = float(timeout_text)
            if not 1 <= llm_timeout_seconds <= 900:
                raise ValueError
        except ValueError:
            errors.append("LLM_TIMEOUT_SECONDS must be between 1 and 900")
            llm_timeout_seconds = 180.0
        if errors:
            raise ValueError("; ".join(errors))
        return cls(
            api_url,
            team_key,
            mcp_endpoint,
            llm_base_url,
            llm_api_key,
            coordinator_model,
            specialist_model,
            verifier_model,
            llm_timeout_seconds,
            resolved_root,
        )
