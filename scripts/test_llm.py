from __future__ import annotations

import asyncio
from pathlib import Path

from student_agent.config import Settings
from student_agent.llm import OpenRouterClient


async def main() -> None:
    settings = Settings.load(Path(__file__).resolve().parents[1])
    client = OpenRouterClient(
        settings.openrouter_api_key, settings.openrouter_base_url, settings.openrouter_model
    )
    try:
        result = await client.complete_json(
            system="Return only a JSON object.",
            user={"task": "Return status ok"},
            max_tokens=100,
        )
    except Exception as exc:
        print(type(exc).__name__, str(exc))
        raise
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
