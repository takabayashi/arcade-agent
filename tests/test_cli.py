"""Unit tests for arcade_agent.cli using typer.testing.CliRunner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from arcade_agent import __version__
from arcade_agent.bindings import Binding, BindingStore
from arcade_agent.cli import app


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable ``dotenv.load_dotenv`` so tests don't pick up a workspace .env file."""
    monkeypatch.setattr("arcade_agent.cli.load_dotenv", lambda *a, **kw: False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_version_prints_installed_version(runner: CliRunner, tmp_config_dir: Path) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.stdout
    assert __version__ in result.stdout


def test_help_lists_all_subcommand_groups(runner: CliRunner, tmp_config_dir: Path) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.stdout
    for needle in ("bind", "daemon", "ask", "chat", "init", "version"):
        assert needle in result.stdout, f"missing {needle!r} in --help output"


def test_daemon_status_no_daemon(runner: CliRunner, tmp_config_dir: Path) -> None:
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0, result.stdout
    assert "not running" in result.stdout


def test_daemon_status_handles_missing_config_dir(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """daemon status must not crash even when CONFIG_DIR does not exist."""
    from arcade_agent import config as _config

    missing = tmp_path / "definitely-not-there"
    monkeypatch.setattr(_config, "CONFIG_DIR", missing)
    monkeypatch.setattr(_config, "BINDINGS_FILE", missing / "bindings.toml")
    monkeypatch.setattr(_config, "SOCKET_PATH", missing / "agent.sock")
    monkeypatch.setattr(_config, "PIDFILE", missing / "daemon.pid")

    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0, result.stdout
    assert "not running" in result.stdout
    assert not missing.exists(), "daemon status should not create the config dir"


def test_daemon_status_stale_pidfile(runner: CliRunner, tmp_config_dir: Path) -> None:
    from arcade_agent import config as _config

    _config.PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    _config.PIDFILE.write_text("99999999\n")
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0, result.stdout
    assert "stale pidfile" in result.stdout


def test_bind_list_empty(runner: CliRunner, tmp_config_dir: Path) -> None:
    result = runner.invoke(app, ["bind", "list"])
    assert result.exit_code == 0, result.stdout
    assert "No bindings yet" in result.stdout


def test_bind_list_with_default(runner: CliRunner, tmp_config_dir: Path) -> None:
    store = BindingStore()
    store.add(Binding(name="personal", user_id="alice@example.com"), make_default=True)
    store.add(Binding(name="work", user_id="bob@work.com"))

    result = runner.invoke(app, ["bind", "list"])
    assert result.exit_code == 0, result.stdout
    assert "personal" in result.stdout
    assert "work" in result.stdout
    # Default marker should appear on the personal row.
    assert "*" in result.stdout


def test_bind_set_default(runner: CliRunner, tmp_config_dir: Path) -> None:
    store = BindingStore()
    store.add(Binding(name="personal", user_id="alice@example.com"), make_default=True)
    store.add(Binding(name="work", user_id="bob@work.com"))

    result = runner.invoke(app, ["bind", "set-default", "work"])
    assert result.exit_code == 0, result.stdout

    fresh = BindingStore()
    assert fresh.get_default().name == "work"


def test_bind_remove_with_yes(runner: CliRunner, tmp_config_dir: Path) -> None:
    store = BindingStore()
    store.add(Binding(name="personal", user_id="alice@example.com"))

    result = runner.invoke(app, ["bind", "remove", "personal", "--yes"])
    assert result.exit_code == 0, result.stdout

    fresh = BindingStore()
    assert fresh.list() == []


def test_bind_add_writes_to_store_and_drives_consent(
    runner: CliRunner,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCADE_API_KEY", "fake-arcade")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic")

    fake_client = MagicMock()
    fake_client.pre_authorize_all = AsyncMock(return_value=[])
    fake_client.wait_for_completion = AsyncMock(return_value=None)
    monkeypatch.setattr("arcade_agent.cli._build_arcade_client", lambda: fake_client)

    result = runner.invoke(app, ["bind", "add", "personal", "--user-id", "alice@example.com"])
    assert result.exit_code == 0, result.stdout
    assert "Saved binding" in result.stdout

    fresh = BindingStore()
    bindings = fresh.list()
    assert [b.name for b in bindings] == ["personal"]
    assert bindings[0].user_id == "alice@example.com"
    fake_client.pre_authorize_all.assert_awaited_once_with("alice@example.com")


def test_bind_add_requires_api_keys(
    runner: CliRunner,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARCADE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = runner.invoke(app, ["bind", "add", "personal", "--user-id", "alice@example.com"])
    assert result.exit_code == 1, result.stdout
    assert "ARCADE_API_KEY" in result.stdout


def test_bind_add_with_pending_consent(
    runner: CliRunner,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcade_agent.arcade_client import AuthURLPending

    monkeypatch.setenv("ARCADE_API_KEY", "fake-arcade")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic")

    pending = [
        AuthURLPending(provider_id="Gmail", auth_id="auth_1", url="https://consent/gmail"),
    ]
    fake_client = MagicMock()
    fake_client.pre_authorize_all = AsyncMock(return_value=pending)
    fake_client.wait_for_completion = AsyncMock(return_value=None)
    monkeypatch.setattr("arcade_agent.cli._build_arcade_client", lambda: fake_client)

    opened: list[str] = []
    monkeypatch.setattr(
        "arcade_agent.cli.webbrowser.open", lambda url, *a, **kw: opened.append(url) or True
    )

    result = runner.invoke(app, ["bind", "add", "personal", "--user-id", "alice@example.com"])
    assert result.exit_code == 0, result.stdout
    assert "https://consent/gmail" in result.stdout
    assert opened == ["https://consent/gmail"]
    fake_client.wait_for_completion.assert_awaited_once_with("auth_1")


def test_ask_no_daemon_prints_friendly_error(runner: CliRunner, tmp_config_dir: Path) -> None:
    store = BindingStore()
    store.add(Binding(name="personal", user_id="alice@example.com"), make_default=True)

    result = runner.invoke(app, ["ask", "hello"])
    assert result.exit_code == 1, result.stdout
    assert "Daemon not running" in result.stdout


def test_ask_resolves_default_binding_when_missing(runner: CliRunner, tmp_config_dir: Path) -> None:
    """If no default binding is set, ask should bail with a friendly hint."""
    result = runner.invoke(app, ["ask", "hello"])
    assert result.exit_code == 1, result.stdout
    assert "default binding" in result.stdout.lower()


def test_ask_unknown_binding_prints_error(runner: CliRunner, tmp_config_dir: Path) -> None:
    result = runner.invoke(app, ["ask", "hello", "--binding", "ghost"])
    assert result.exit_code == 1, result.stdout
    assert "ghost" in result.stdout


def test_ask_streams_events_to_completion(
    runner: CliRunner,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BindingStore()
    store.add(Binding(name="personal", user_id="alice@example.com"), make_default=True)

    sent_lines: list[bytes] = []
    scripted_events: list[dict[str, Any]] = [
        {"event": "text", "delta": "Hello "},
        {"event": "text", "delta": "world!"},
        {"event": "final", "text": "Hello world!"},
    ]

    class FakeReader:
        def __init__(self, events: list[dict[str, Any]]) -> None:
            self._queue: list[bytes] = [(json.dumps(e) + "\n").encode() for e in events]

        async def readline(self) -> bytes:
            if not self._queue:
                return b""
            return self._queue.pop(0)

    class FakeWriter:
        def __init__(self) -> None:
            self.closed = False

        def write(self, data: bytes) -> None:
            sent_lines.append(data)

        async def drain(self) -> None:
            await asyncio.sleep(0)

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            await asyncio.sleep(0)

    async def fake_open(path: str) -> tuple[FakeReader, FakeWriter]:
        assert path  # exercised
        return FakeReader(scripted_events), FakeWriter()

    monkeypatch.setattr("arcade_agent.cli.asyncio.open_unix_connection", fake_open)

    result = runner.invoke(app, ["ask", "say hi"])
    assert result.exit_code == 0, result.stdout
    assert "Hello" in result.stdout
    assert "world" in result.stdout

    assert sent_lines, "the CLI must send at least one prompt line"
    first_msg = json.loads(sent_lines[0].decode())
    assert first_msg["op"] == "prompt"
    assert first_msg["binding"] == "personal"
    assert first_msg["prompt"] == "say hi"
    assert "session_id" in first_msg


def test_ask_with_confirm_event_sends_ack(
    runner: CliRunner,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = BindingStore()
    store.add(Binding(name="personal", user_id="alice@example.com"), make_default=True)

    sent_lines: list[bytes] = []
    scripted_events: list[dict[str, Any]] = [
        {
            "event": "confirm",
            "tool": "Gmail.SendEmail",
            "args": {"to": "x@y"},
            "session_id": "irrelevant",
        },
        {"event": "tool_result", "name": "Gmail.SendEmail", "ok": True, "summary": "sent"},
        {"event": "final", "text": "done"},
    ]

    class FakeReader:
        def __init__(self, events: list[dict[str, Any]]) -> None:
            self._queue: list[bytes] = [(json.dumps(e) + "\n").encode() for e in events]

        async def readline(self) -> bytes:
            if not self._queue:
                return b""
            return self._queue.pop(0)

    class FakeWriter:
        def write(self, data: bytes) -> None:
            sent_lines.append(data)

        async def drain(self) -> None:
            await asyncio.sleep(0)

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            await asyncio.sleep(0)

    async def fake_open(path: str) -> tuple[FakeReader, FakeWriter]:
        return FakeReader(scripted_events), FakeWriter()

    monkeypatch.setattr("arcade_agent.cli.asyncio.open_unix_connection", fake_open)
    # Auto-approve the confirm prompt.
    monkeypatch.setattr("arcade_agent.cli.Confirm.ask", lambda *a, **kw: True)

    result = runner.invoke(app, ["ask", "send"])
    assert result.exit_code == 0, result.stdout

    parsed = [json.loads(line.decode()) for line in sent_lines]
    confirm_acks = [p for p in parsed if p.get("op") == "confirm_ack"]
    assert len(confirm_acks) == 1
    assert confirm_acks[0]["approved"] is True
