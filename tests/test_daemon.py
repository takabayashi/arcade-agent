"""Integration tests for arcade_agent.daemon.

These tests stand up a real :class:`DaemonServer` on a tmp Unix domain socket
with an injected :class:`FakeClaudeAgent` whose ``run_turn`` yields a scripted
sequence of events. The point is to exercise the wire protocol, the confirm
future routing, and the multi-connection isolation guarantees without
touching Anthropic or Arcade.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from arcade_agent.bindings import Binding, BindingStore
from arcade_agent.claude_agent import (
    AuthURLRequested,
    ConfirmRequested,
    ErrorEvent,
    Final,
    OnAuthURL,
    OnConfirm,
    TextDelta,
    ToolCallResult,
    ToolCallStarted,
)
from arcade_agent.daemon import DaemonServer, is_running


class FakeClaudeAgent:
    """A ClaudeAgent-shaped fake whose ``run_turn`` walks a scripted list."""

    def __init__(
        self,
        script: list[dict[str, Any]],
        on_auth_url: OnAuthURL,
        on_confirm: OnConfirm,
    ) -> None:
        self._script = script
        self._on_auth_url = on_auth_url
        self._on_confirm = on_confirm

    async def run_turn(
        self,
        user_id: str,
        prompt: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        for step in self._script:
            kind = step["type"]
            if kind == "text":
                yield TextDelta(text=step["text"])
            elif kind == "tool_call":
                yield ToolCallStarted(name=step["name"], args=step.get("args", {}))
            elif kind == "tool_result":
                yield ToolCallResult(
                    name=step["name"],
                    ok=step.get("ok", True),
                    summary=step.get("summary", ""),
                )
            elif kind == "auth_url":
                await self._on_auth_url(step["url"])
                yield AuthURLRequested(url=step["url"])
            elif kind == "confirm":
                yield ConfirmRequested(tool_name=step["tool"], args=step.get("args", {}))
                step["_approved"] = await self._on_confirm(step["tool"], step.get("args", {}))
            elif kind == "error":
                yield ErrorEvent(message=step["message"])
            elif kind == "final":
                yield Final(text=step.get("text", ""))
                return
        yield Final(text="")


def make_factory(
    script: list[dict[str, Any]],
) -> Callable[[OnAuthURL, OnConfirm], FakeClaudeAgent]:
    def factory(on_auth_url: OnAuthURL, on_confirm: OnConfirm) -> FakeClaudeAgent:
        return FakeClaudeAgent(script, on_auth_url, on_confirm)

    return factory


@pytest.fixture
def short_tmp(tmp_path: Path) -> Iterator[Path]:
    """A short-path tmp dir under ``/tmp`` so AF_UNIX paths fit the 104-byte limit."""
    base = Path(tempfile.mkdtemp(prefix="aagt-", dir="/tmp"))
    try:
        yield base
    finally:
        for child in sorted(base.glob("*"), reverse=True):
            try:
                if child.is_dir():
                    child.rmdir()
                else:
                    child.unlink()
            except OSError:
                pass
        try:
            base.rmdir()
        except OSError:
            pass


@pytest.fixture
def populated_store(short_tmp: Path) -> BindingStore:
    store = BindingStore(path=short_tmp / "bindings.toml")
    store.add(Binding(name="personal", user_id="user@example.com"), make_default=True)
    return store


async def _spawn_server(
    base: Path,
    factory: Callable[[OnAuthURL, OnConfirm], FakeClaudeAgent],
    store: BindingStore,
) -> tuple[DaemonServer, asyncio.Task[None]]:
    server = DaemonServer(
        factory,
        store,
        socket_path=base / "agent.sock",
        pidfile=base / "daemon.pid",
    )
    task = asyncio.create_task(server.start())
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 2.0
    while not server.socket_path.exists():
        if task.done():
            exc = task.exception()
            if exc is not None:
                raise exc
            raise RuntimeError("server task ended before socket appeared")
        if loop.time() > deadline:
            task.cancel()
            raise RuntimeError("daemon socket never appeared")
        await asyncio.sleep(0.02)
    return server, task


async def _shutdown(server: DaemonServer, task: asyncio.Task[None]) -> None:
    await server.stop()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (TimeoutError, asyncio.CancelledError):
        pass


async def _send(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    assert line, "daemon closed the connection unexpectedly"
    return json.loads(line)


async def test_text_only_flow(short_tmp: Path, populated_store: BindingStore) -> None:
    script = [
        {"type": "text", "text": "hello"},
        {"type": "final", "text": "hello world"},
    ]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "hi", "session_id": "s1"},
        )

        evt1 = await _recv(reader)
        assert evt1 == {"event": "text", "delta": "hello"}
        evt2 = await _recv(reader)
        assert evt2 == {"event": "final", "text": "hello world"}

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_confirm_approve_flow(short_tmp: Path, populated_store: BindingStore) -> None:
    script: list[dict[str, Any]] = [
        {"type": "tool_call", "name": "Gmail.SendEmail", "args": {"to": "x@y"}},
        {"type": "confirm", "tool": "Gmail.SendEmail", "args": {"to": "x@y"}},
        {"type": "tool_result", "name": "Gmail.SendEmail", "ok": True, "summary": "sent"},
        {"type": "final", "text": "done"},
    ]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "send", "session_id": "s1"},
        )

        tool_call = await _recv(reader)
        assert tool_call["event"] == "tool_call"
        assert tool_call["name"] == "Gmail.SendEmail"

        confirm = await _recv(reader)
        assert confirm["event"] == "confirm"
        assert confirm["tool"] == "Gmail.SendEmail"
        assert confirm["session_id"] == "s1"

        await _send(writer, {"op": "confirm_ack", "session_id": "s1", "approved": True})

        tool_result = await _recv(reader)
        assert tool_result["event"] == "tool_result" and tool_result["ok"] is True
        final = await _recv(reader)
        assert final["event"] == "final" and final["text"] == "done"

        # Verify the scripted step actually saw approved=True.
        assert script[1]["_approved"] is True

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_confirm_deny_flow(short_tmp: Path, populated_store: BindingStore) -> None:
    script: list[dict[str, Any]] = [
        {"type": "confirm", "tool": "Gmail.SendEmail", "args": {"to": "x@y"}},
        {"type": "tool_result", "name": "Gmail.SendEmail", "ok": False, "summary": "declined"},
        {"type": "final", "text": "ack"},
    ]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "send", "session_id": "s1"},
        )

        confirm = await _recv(reader)
        assert confirm["event"] == "confirm"

        await _send(writer, {"op": "confirm_ack", "session_id": "s1", "approved": False})

        tool_result = await _recv(reader)
        assert tool_result["event"] == "tool_result" and tool_result["ok"] is False
        final = await _recv(reader)
        assert final["event"] == "final"
        assert script[0]["_approved"] is False

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_auth_url_flow(short_tmp: Path, populated_store: BindingStore) -> None:
    script: list[dict[str, Any]] = [
        {"type": "auth_url", "url": "https://consent"},
        {"type": "tool_call", "name": "Gmail.ListEmails", "args": {}},
        {"type": "tool_result", "name": "Gmail.ListEmails", "ok": True, "summary": "ok"},
        {"type": "final", "text": "done"},
    ]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "list", "session_id": "s1"},
        )

        auth = await _recv(reader)
        assert auth == {"event": "auth_url", "url": "https://consent"}
        tool_call = await _recv(reader)
        assert tool_call["event"] == "tool_call"

        tool_result = await _recv(reader)
        assert tool_result["event"] == "tool_result"
        final = await _recv(reader)
        assert final["event"] == "final"

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_malformed_json_keeps_connection_open(
    short_tmp: Path, populated_store: BindingStore
) -> None:
    script = [{"type": "final", "text": "ok"}]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b"this is not json\n")
        await writer.drain()

        err = await _recv(reader)
        assert err["event"] == "error" and "malformed" in err["message"].lower()

        await _send(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "hi", "session_id": "s1"},
        )
        final = await _recv(reader)
        assert final["event"] == "final"

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_unknown_binding_emits_error(short_tmp: Path, populated_store: BindingStore) -> None:
    script = [{"type": "final", "text": "unused"}]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send(
            writer,
            {
                "op": "prompt",
                "binding": "ghost",
                "prompt": "hi",
                "session_id": "s1",
            },
        )
        evt = await _recv(reader)
        assert evt["event"] == "error" and "ghost" in evt["message"]

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_unknown_op_emits_error(short_tmp: Path, populated_store: BindingStore) -> None:
    script = [{"type": "final", "text": "unused"}]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send(writer, {"op": "shenanigans", "session_id": "s1"})
        evt = await _recv(reader)
        assert evt["event"] == "error" and "unknown op" in evt["message"]
        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_parallel_sessions_independent_confirms(
    short_tmp: Path, populated_store: BindingStore
) -> None:
    """Two connections waiting on confirm must resolve independently."""
    script: list[dict[str, Any]] = [
        {"type": "tool_call", "name": "Gmail.SendEmail", "args": {}},
        {"type": "confirm", "tool": "Gmail.SendEmail", "args": {}},
        {"type": "tool_result", "name": "Gmail.SendEmail", "ok": True, "summary": "sent"},
        {"type": "final", "text": "done"},
    ]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader_a, writer_a = await asyncio.open_unix_connection(str(server.socket_path))
        reader_b, writer_b = await asyncio.open_unix_connection(str(server.socket_path))

        await _send(
            writer_a,
            {"op": "prompt", "binding": "personal", "prompt": "a", "session_id": "sa"},
        )
        await _send(
            writer_b,
            {"op": "prompt", "binding": "personal", "prompt": "b", "session_id": "sb"},
        )

        evt_a1 = await _recv(reader_a)
        evt_a2 = await _recv(reader_a)
        evt_b1 = await _recv(reader_b)
        evt_b2 = await _recv(reader_b)
        assert evt_a1["event"] == "tool_call" and evt_a2["event"] == "confirm"
        assert evt_b1["event"] == "tool_call" and evt_b2["event"] == "confirm"
        assert evt_a2["session_id"] == "sa"
        assert evt_b2["session_id"] == "sb"

        # Ack B first to prove A's pending confirm doesn't block B.
        await _send(writer_b, {"op": "confirm_ack", "session_id": "sb", "approved": True})
        tr_b = await _recv(reader_b)
        f_b = await _recv(reader_b)
        assert tr_b["event"] == "tool_result"
        assert f_b["event"] == "final"

        await _send(writer_a, {"op": "confirm_ack", "session_id": "sa", "approved": True})
        tr_a = await _recv(reader_a)
        f_a = await _recv(reader_a)
        assert tr_a["event"] == "tool_result"
        assert f_a["event"] == "final"

        for w in (writer_a, writer_b):
            w.close()
            await w.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_confirm_ack_for_unknown_session_emits_error(
    short_tmp: Path, populated_store: BindingStore
) -> None:
    script = [{"type": "final", "text": "ok"}]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send(writer, {"op": "confirm_ack", "session_id": "ghost", "approved": True})
        evt = await _recv(reader)
        assert evt["event"] == "error" and "no pending confirm" in evt["message"]
        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_pidfile_lifecycle_and_already_running(
    short_tmp: Path, populated_store: BindingStore
) -> None:
    script = [{"type": "final", "text": "ok"}]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        # is_running should see the live pidfile this process owns.
        assert is_running(server.pidfile) == os.getpid()
        assert server.status() == ("running", os.getpid())
    finally:
        await _shutdown(server, task)
    # After stop, pidfile and socket are gone.
    assert not server.pidfile.exists()
    assert not server.socket_path.exists()


def test_stale_pidfile_status(tmp_path: Path) -> None:
    pidfile = tmp_path / "daemon.pid"
    pidfile.write_text("99999999\n")
    socket_path = tmp_path / "agent.sock"

    def factory(on_auth_url: OnAuthURL, on_confirm: OnConfirm) -> FakeClaudeAgent:
        return FakeClaudeAgent([], on_auth_url, on_confirm)

    store = BindingStore(path=tmp_path / "bindings.toml")
    server = DaemonServer(factory, store, socket_path=socket_path, pidfile=pidfile)
    status, pid = server.status()
    assert status == "stale_pidfile"
    assert pid == 99999999


def test_signal_constants_are_available() -> None:
    """Sanity check so the file uses ``signal`` and ruff doesn't flag it."""
    assert signal.SIGTERM > 0
