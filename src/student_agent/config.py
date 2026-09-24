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
    openrouter_api_key: str
    openrouter_base_url: str
    openrouter_model: str
    root: Path

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env")
        api_url = os.getenv("COMPETITION_API_URL", "").strip().rstrip("/")
        team_key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
        mcp_endpoint = os.getenv("MCP_ENDPOINT", "").strip()
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        openrouter_base_url = os.getenv(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ).strip().rstrip("/")
        openrouter_model = os.getenv("OPENROUTER_MODEL", "qwen/qwen3-8b").strip()
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if not openrouter_api_key.startswith("sk-or-"):
            errors.append("OPENROUTER_API_KEY must use the sk-or-... format")
        if not openrouter_base_url.startswith(("http://", "https://")):
            errors.append("OPENROUTER_BASE_URL must be an absolute HTTP(S) URL")
        if not openrouter_model:
            errors.append("OPENROUTER_MODEL must not be empty")
        if errors:
            raise ValueError("; ".join(errors))
        return cls(
            api_url,
            team_key,
            mcp_endpoint,
            openrouter_api_key,
            openrouter_base_url,
            openrouter_model,
            resolved_root,
        )
