"""Live smoke probe against Arcade for the configured Gmail + GoogleCalendar tools.

Run manually after secrets are in place::

    ARCADE_API_KEY=... .venv/bin/python scripts/probe.py

Exits 0 if both ``Gmail`` and ``GoogleCalendar`` toolkits return at least one
ToolDefinition we know about; exits 1 with a clear message otherwise. The
script is **not** a pytest target: keep it usable as a one-shot diagnostic.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterable
from typing import Any

from dotenv import load_dotenv


def _trunc(value: str | None, limit: int = 80) -> str:
    text = (value or "").splitlines()[0] if value else ""
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


async def main() -> int:
    load_dotenv()
    api_key = os.environ.get("ARCADE_API_KEY")
    if not api_key:
        print(
            "ERROR: ARCADE_API_KEY missing. Set it in your environment or .env file.",
            file=sys.stderr,
        )
        return 1

    import arcadepy

    from arcade_agent import config
    from arcade_agent.arcade_client import ArcadeAgentClient

    client = arcadepy.AsyncArcade(api_key=api_key)
    wrapper = ArcadeAgentClient(client, config.ALL_TOOLS)

    try:
        schemas: Iterable[dict[str, Any]] = await wrapper.list_tool_schemas()
    except Exception as exc:
        print(f"ERROR: list_tool_schemas failed: {exc}", file=sys.stderr)
        return 1

    schemas_list = list(schemas)
    if not schemas_list:
        print("ERROR: Arcade returned no tool schemas for the configured tools.", file=sys.stderr)
        return 1

    arcade_to_anthropic = wrapper._arcade_to_anthropic
    print(f"Discovered {len(schemas_list)} tools:")
    for arcade_name in config.ALL_TOOLS:
        anthropic_name = arcade_to_anthropic.get(arcade_name)
        if anthropic_name is None:
            continue
        schema = next((s for s in schemas_list if s["name"] == anthropic_name), None)
        if schema is None:
            continue
        description = _trunc(schema.get("description"))
        print(f"  {arcade_name}")
        print(f"    anthropic_name: {anthropic_name}")
        print(f"    description:    {description}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
