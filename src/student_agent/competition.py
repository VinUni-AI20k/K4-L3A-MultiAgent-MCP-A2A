"""Competition API: open (or resume) the team's active run for a variant.

MCP tools only return evidence while the team has an active run, and submitted
evidence refs must belong to that run. The workspace page performs the same call
(`POST /api/v2/runs`) when a team opens it.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx2

from . import VARIANT_ID
from .config import Settings


@dataclass(frozen=True)
class ActiveRun:
    variant_id: str
    case_set_version: str
    expires_at: str | None
    mcp_endpoint: str | None


def open_run(settings: Settings, variant_id: str = VARIANT_ID) -> ActiveRun:
    url = f"{settings.competition_api_url}/api/v2/runs"
    last_error = "unknown error"
    for _ in range(3):
        try:
            response = httpx2.post(
                url,
                json={"variant_id": variant_id},
                headers={"Authorization": f"Bearer {settings.team_api_key}"},
                timeout=30.0,
            )
        except httpx2.TransportError as exc:
            last_error = type(exc).__name__
            continue
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"competition API rejected the team key (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            continue
        body = response.json()
        return ActiveRun(
            variant_id=body.get("variant_id", variant_id),
            case_set_version=body.get("case_set_version", ""),
            expires_at=body.get("expires_at"),
            mcp_endpoint=body.get("mcp_endpoint"),
        )
    raise RuntimeError(f"could not open competition run: {last_error}")
