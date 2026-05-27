"""Filesystem paths, default model, and the toolkit list this agent exposes to Claude."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR: Path = Path(os.environ.get("ARCADE_AGENT_HOME", Path.home() / ".arcade-agent"))
BINDINGS_FILE: Path = CONFIG_DIR / "bindings.toml"
SOCKET_PATH: Path = CONFIG_DIR / "agent.sock"
PIDFILE: Path = CONFIG_DIR / "daemon.pid"

DEFAULT_ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

GMAIL_TOOLS: list[str] = [
    "Gmail.SendEmail",
    "Gmail.ListEmails",
    "Gmail.SearchThreads",
    "Gmail.GetThread",
    "Gmail.WriteDraftEmail",
    "Gmail.ReplyToEmail",
    "Gmail.ListLabels",
]

GCAL_TOOLS: list[str] = [
    "GoogleCalendar.ListEvents",
    "GoogleCalendar.CreateEvent",
    "GoogleCalendar.UpdateEvent",
    "GoogleCalendar.DeleteEvent",
    "GoogleCalendar.FindTimeSlotsWhenEveryoneIsFree",
    "GoogleCalendar.ListCalendars",
]

ALL_TOOLS: list[str] = [*GMAIL_TOOLS, *GCAL_TOOLS]

DESTRUCTIVE_TOOLS: set[str] = {
    "Gmail.SendEmail",
    "GoogleCalendar.DeleteEvent",
    "GoogleCalendar.UpdateEvent",
}


def ensure_config_dir() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR
