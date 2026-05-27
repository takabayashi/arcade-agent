"""Additional QA coverage authored during the adversarial defect audit.

Passing tests document behaviors that the QA verified as provably correct.
Skipped tests carry a ``QA-DEFECT-N`` marker so a fixer subagent can correlate
the failing scenario with the entry of the same name in ``docs/DEFECTS.md``.

The skipped tests are written so that removing the ``skip`` decorator after the
defect is fixed will execute the intended assertion and confirm the fix.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from arcade_agent import config
from arcade_agent.arcade_client import (
    ArcadeAgentClient,
    ExecuteResult,
    _normalize_name,
)
from arcade_agent.bindings import Binding, BindingStore
from arcade_agent.claude_agent import (
    AuthURLRequested,
    ClaudeAgent,
    ConfirmRequested,
    ErrorEvent,
    Final,
    OnAuthURL,
    OnConfirm,
    TextDelta,
    ToolCallResult,
    ToolCallStarted,
)
from arcade_agent.cli import app
from arcade_agent.daemon import DaemonServer

# ----------------------------------------------------------------------------
# Shared helpers (close to test_daemon.py's, copied to keep this file standalone)
# ----------------------------------------------------------------------------


class FakeClaudeAgent:
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
        history.append({"role": "user", "content": prompt})
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
                history.append({"role": "assistant", "content": step.get("text", "")})
                return
        yield Final(text="")
        history.append({"role": "assistant", "content": ""})


def make_factory(
    script: list[dict[str, Any]],
) -> Callable[[OnAuthURL, OnConfirm], FakeClaudeAgent]:
    def factory(on_auth_url: OnAuthURL, on_confirm: OnConfirm) -> FakeClaudeAgent:
        return FakeClaudeAgent(list(script), on_auth_url, on_confirm)

    return factory


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    base = Path(tempfile.mkdtemp(prefix="qa-aagt-", dir="/tmp"))
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
    factory: Callable[[OnAuthURL, OnConfirm], Any],
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


async def _send_line(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()


async def _recv_line(reader: asyncio.StreamReader, timeout: float = 5.0) -> dict[str, Any]:
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    assert line, "daemon closed the connection unexpectedly"
    return json.loads(line)


# ----------------------------------------------------------------------------
# Passing tests — confirm correctness of behaviors that DID hold up.
# ----------------------------------------------------------------------------


def test_qa_all_configured_tool_names_satisfy_anthropic_constraint() -> None:
    """Every Arcade tool name in config.ALL_TOOLS must normalize to a valid
    Anthropic tool name (regex ^[a-zA-Z0-9_-]{1,64}$).
    """
    name_re = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
    for arcade_name in config.ALL_TOOLS:
        anthropic_name = _normalize_name(arcade_name)
        assert name_re.match(anthropic_name), (
            f"Arcade tool {arcade_name!r} normalizes to {anthropic_name!r}, "
            "which violates the Anthropic tool-name regex."
        )
        assert len(anthropic_name) <= 64
        assert "." not in anthropic_name


def test_qa_destructive_tools_subset_of_all_tools() -> None:
    """Every DESTRUCTIVE_TOOLS entry must be a real entry in ALL_TOOLS."""
    missing = set(config.DESTRUCTIVE_TOOLS) - set(config.ALL_TOOLS)
    assert missing == set(), (
        f"DESTRUCTIVE_TOOLS references tools that are not in ALL_TOOLS: {missing}"
    )


async def test_qa_claude_agent_tool_failure_feeds_error_back_to_model(
    mock_arcade: MagicMock,
    mock_anthropic: MagicMock,
) -> None:
    """When Arcade returns success=False with an error_message, the agent must
    surface a ToolCallResult(ok=False) AND feed the error string back to Claude
    as an is_error=True tool_result so the model can choose to retry or apologise.
    """
    arcade_client = ArcadeAgentClient(mock_arcade, ["Gmail.ListEmails"])
    arcade_client._anthropic_to_arcade = {"Gmail_ListEmails": "Gmail.ListEmails"}
    arcade_client._arcade_to_anthropic = {"Gmail.ListEmails": "Gmail_ListEmails"}
    arcade_client._schemas = []

    arcade_client.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecuteResult(
            success=False,
            output=None,
            auth_url=None,
            error_message="upstream 500: Gmail down",
            error_kind="UPSTREAM_RUNTIME_SERVER_ERROR",
        )
    )

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_ListEmails", {}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Sorry, Gmail is unavailable right now."),
        stop_reason="end_turn",
    )
    create = AsyncMock(side_effect=[msg1, msg2])
    mock_anthropic.messages.create = create

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=AsyncMock(return_value=True),
    )

    history: list[dict[str, Any]] = []
    events = [ev async for ev in agent.run_turn("user@x", "list", history)]

    failed_results = [e for e in events if isinstance(e, ToolCallResult) and not e.ok]
    assert failed_results, "agent must surface a failed ToolCallResult"
    assert "upstream 500" in failed_results[0].summary

    second_call = create.await_args_list[1]
    history_arg = second_call.kwargs["messages"]
    tool_result_msgs = [
        m for m in history_arg if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert tool_result_msgs, "tool_result message was never fed back to Claude"
    tool_result_blocks = [
        block
        for msg in tool_result_msgs
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert any(b.get("is_error") is True for b in tool_result_blocks)
    assert any("upstream 500" in (b.get("content") or "") for b in tool_result_blocks)


async def test_qa_daemon_multi_prompt_same_session_preserves_history(
    short_tmp: Path,
    populated_store: BindingStore,
) -> None:
    """Two sequential prompts on the same connection + session_id must end up
    in the same history list (i.e. multi-turn chat works).
    """
    histories_seen: list[list[dict[str, Any]]] = []

    class RecordingAgent:
        def __init__(self, on_auth_url: OnAuthURL, on_confirm: OnConfirm) -> None:
            pass

        async def run_turn(
            self,
            user_id: str,
            prompt: str,
            history: list[dict[str, Any]],
        ) -> AsyncIterator[Any]:
            histories_seen.append(list(history))
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": f"ack:{prompt}"})
            yield Final(text=f"ack:{prompt}")

    def factory(on_auth_url: OnAuthURL, on_confirm: OnConfirm) -> RecordingAgent:
        return RecordingAgent(on_auth_url, on_confirm)

    server, task = await _spawn_server(short_tmp, factory, populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        for prompt in ("hi", "again"):
            await _send_line(
                writer,
                {"op": "prompt", "binding": "personal", "prompt": prompt, "session_id": "S"},
            )
            evt = await _recv_line(reader)
            assert evt["event"] == "final"

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)

    assert len(histories_seen) == 2, "expected two run_turn invocations"
    assert histories_seen[0] == [], "first prompt should see empty history"
    second_msgs = histories_seen[1]
    assert any(m.get("role") == "user" and m.get("content") == "hi" for m in second_msgs), (
        f"second prompt should see prior user msg in history; got {second_msgs!r}"
    )
    assert any(
        m.get("role") == "assistant" and "ack:hi" in (m.get("content") or "") for m in second_msgs
    ), f"second prompt should see prior assistant reply; got {second_msgs!r}"


async def test_qa_daemon_client_disconnect_midturn_no_pending_confirm_leak(
    short_tmp: Path,
    populated_store: BindingStore,
) -> None:
    """If the client drops the connection while a confirm is pending, the
    daemon must clean up the pending future and stay healthy for new clients.
    """
    script: list[dict[str, Any]] = [
        {"type": "tool_call", "name": "Gmail.SendEmail", "args": {}},
        {"type": "confirm", "tool": "Gmail.SendEmail", "args": {}},
        {"type": "final", "text": "done"},
    ]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send_line(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "send", "session_id": "S"},
        )
        evt = await _recv_line(reader)
        assert evt["event"] == "tool_call"
        evt = await _recv_line(reader)
        assert evt["event"] == "confirm"

        writer.close()
        await writer.wait_closed()

        await asyncio.sleep(0.2)
        assert "S" not in server._pending_confirms, (
            "pending confirm future leaked after client disconnect"
        )

        reader2, writer2 = await asyncio.open_unix_connection(str(server.socket_path))
        await _send_line(
            writer2,
            {"op": "prompt", "binding": "personal", "prompt": "send", "session_id": "S2"},
        )
        evt = await _recv_line(reader2)
        assert evt["event"] == "tool_call"
        writer2.close()
        await writer2.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_qa_daemon_recovers_after_invalid_confirm_ack_shape(
    short_tmp: Path,
    populated_store: BindingStore,
) -> None:
    """A confirm_ack missing 'session_id' or 'approved' is rejected but the
    connection stays open and subsequent valid messages still work.
    """
    script: list[dict[str, Any]] = [{"type": "final", "text": "ok"}]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))

        await _send_line(writer, {"op": "confirm_ack", "approved": True})
        evt = await _recv_line(reader)
        assert evt["event"] == "error" and "session_id" in evt["message"]

        await _send_line(writer, {"op": "confirm_ack", "session_id": "S"})
        evt = await _recv_line(reader)
        assert evt["event"] == "error" and "approved" in evt["message"]

        await _send_line(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "hi", "session_id": "S"},
        )
        final = await _recv_line(reader)
        assert final["event"] == "final"

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_qa_daemon_handles_non_object_inbound_json(
    short_tmp: Path,
    populated_store: BindingStore,
) -> None:
    """Non-object JSON (an array, a string, a number) must be rejected without
    crashing the connection.
    """
    script: list[dict[str, Any]] = [{"type": "final", "text": "ok"}]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b'["op", "prompt"]\n')
        await writer.drain()
        evt = await _recv_line(reader)
        assert evt["event"] == "error" and "object" in evt["message"].lower()

        writer.write(b'"just a string"\n')
        await writer.drain()
        evt = await _recv_line(reader)
        assert evt["event"] == "error"

        await _send_line(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "hi", "session_id": "S"},
        )
        final = await _recv_line(reader)
        assert final["event"] == "final"

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


def test_qa_cli_bind_list_when_bindings_file_missing(
    tmp_config_dir: Path,
) -> None:
    """`bind list` must succeed and not crash when the bindings TOML doesn't
    exist yet (fresh-install scenario).
    """
    assert not (tmp_config_dir / "bindings.toml").exists()
    runner = CliRunner()
    result = runner.invoke(app, ["bind", "list"])
    assert result.exit_code == 0, result.stdout
    assert "No bindings yet" in result.stdout


def test_qa_cli_ask_when_daemon_not_running_exits_nonzero(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ask` must exit non-zero with a friendly message when no daemon is up."""
    monkeypatch.setattr("arcade_agent.cli.load_dotenv", lambda *a, **kw: False)
    store = BindingStore()
    store.add(Binding(name="personal", user_id="user@example.com"), make_default=True)

    runner = CliRunner()
    result = runner.invoke(app, ["ask", "hi"])
    assert result.exit_code == 1
    assert "Daemon not running" in result.stdout


def test_qa_bindings_file_writes_with_strict_permissions(tmp_path: Path) -> None:
    """The on-disk bindings file must end up mode 0600 even after multiple
    edits (so secrets-shaped fields can later land there safely).
    """
    store = BindingStore(path=tmp_path / "bindings.toml")
    store.add(Binding(name="a", user_id="a@x"))
    store.add(Binding(name="b", user_id="b@x"))
    store.set_default("b")
    store.remove("a")
    mode = os.stat(store.path).st_mode & 0o777
    assert mode == 0o600


def test_qa_no_print_in_daemon_or_claude_agent() -> None:
    """Spec invariant I12: no print() calls inside daemon.py / claude_agent.py."""
    here = Path(__file__).resolve().parent.parent / "src" / "arcade_agent"
    for fname in ("daemon.py", "claude_agent.py", "arcade_client.py", "bindings.py", "confirm.py"):
        text = (here / fname).read_text()
        without_strings = re.sub(r'"[^"]*"|\'[^\']*\'', "", text)
        without_strings = re.sub(r"#.*", "", without_strings)
        assert not re.search(r"\bprint\s*\(", without_strings), f"unexpected print() in {fname}"


def test_qa_every_source_file_imports_future_annotations() -> None:
    """Spec invariant I11: every package module starts with `from __future__ import annotations`."""
    here = Path(__file__).resolve().parent.parent / "src" / "arcade_agent"
    skip = {"__init__.py"}
    for path in sorted(here.glob("*.py")):
        if path.name in skip:
            continue
        text = path.read_text()
        assert "from __future__ import annotations" in text, (
            f"{path.name} missing `from __future__ import annotations`"
        )


# ----------------------------------------------------------------------------
# Live-network probe. Skipped by default. Documents the CRITICAL Arcade-name
# mismatch.
# ----------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("ARCADE_API_KEY") is None,
    reason="needs live ARCADE_API_KEY",
)
async def test_qa_defect_1_live_arcade_names_match_config() -> None:
    import arcadepy

    api_key = os.environ.get("ARCADE_API_KEY")
    assert api_key, "ARCADE_API_KEY required for live probe"
    client = arcadepy.AsyncArcade(api_key=api_key)
    wrapper = ArcadeAgentClient(client, list(config.ALL_TOOLS))
    await wrapper.list_tool_schemas()


# ----------------------------------------------------------------------------
# QA-DEFECT-marked SKIP tests — documented assertions that would FAIL today
# against the real claude_agent code path. Removing the skip after the fix is
# enough to validate the fix.
# ----------------------------------------------------------------------------


async def test_qa_defect_2_wire_order_tool_call_before_confirm(
    mock_arcade: MagicMock,
    mock_anthropic: MagicMock,
) -> None:
    arcade_client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    arcade_client._anthropic_to_arcade = {"Gmail_SendEmail": "Gmail.SendEmail"}
    arcade_client._arcade_to_anthropic = {"Gmail.SendEmail": "Gmail_SendEmail"}
    arcade_client._schemas = []
    arcade_client.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecuteResult(
            success=True,
            output={"id": "ok"},
            auth_url=None,
            error_message=None,
            error_kind=None,
        )
    )

    log: list[str] = []

    async def on_confirm(name: str, args: dict[str, Any]) -> bool:
        log.append(f"on_confirm:{name}")
        return True

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_SendEmail", {"to": "a@b"}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Sent."), stop_reason="end_turn"
    )
    mock_anthropic.messages.create = AsyncMock(side_effect=[msg1, msg2])

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=on_confirm,
    )
    async for ev in agent.run_turn("u@x", "send", []):
        if isinstance(ev, ToolCallStarted):
            log.append(f"yield:ToolCallStarted:{ev.name}")
        elif isinstance(ev, ConfirmRequested):
            log.append(f"yield:ConfirmRequested:{ev.tool_name}")
        elif isinstance(ev, ToolCallResult):
            log.append(f"yield:ToolCallResult:{ev.name}:ok={ev.ok}")

    tc_idx = next(i for i, e in enumerate(log) if e.startswith("yield:ToolCallStarted"))
    cb_idx = next(i for i, e in enumerate(log) if e.startswith("on_confirm:"))
    assert tc_idx < cb_idx, (
        f"ToolCallStarted must be yielded BEFORE on_confirm is invoked. log={log}"
    )


async def test_qa_defect_3_on_confirm_raise_aborts_turn(
    mock_arcade: MagicMock,
    mock_anthropic: MagicMock,
) -> None:
    arcade_client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    arcade_client._anthropic_to_arcade = {"Gmail_SendEmail": "Gmail.SendEmail"}
    arcade_client._arcade_to_anthropic = {"Gmail.SendEmail": "Gmail_SendEmail"}
    arcade_client._schemas = []

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_SendEmail", {}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Sent."), stop_reason="end_turn"
    )
    create = AsyncMock(side_effect=[msg1, msg2])
    mock_anthropic.messages.create = create

    async def boom(*args: Any, **kw: Any) -> bool:
        raise RuntimeError("confirm gate exploded")

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=boom,
    )
    events = [ev async for ev in agent.run_turn("u@x", "send", [])]
    assert create.await_count == 1, (
        "spec requires the turn to abort: Claude must not be called a second time"
    )
    finals = [e for e in events if isinstance(e, Final)]
    assert finals and finals[-1].text == "", (
        "spec requires Final(text='') after the ErrorEvent when on_confirm raises"
    )


async def test_qa_defect_3_on_auth_url_raise_aborts_turn(
    mock_arcade: MagicMock,
    mock_anthropic: MagicMock,
) -> None:
    arcade_client = ArcadeAgentClient(mock_arcade, ["Gmail.ListEmails"])
    arcade_client._anthropic_to_arcade = {"Gmail_ListEmails": "Gmail.ListEmails"}
    arcade_client._arcade_to_anthropic = {"Gmail.ListEmails": "Gmail_ListEmails"}
    arcade_client._schemas = []
    arcade_client.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecuteResult(
            success=False,
            output=None,
            auth_url="https://consent",
            error_message=None,
            error_kind="AUTH_REQUIRED",
        )
    )

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_ListEmails", {}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Listed."), stop_reason="end_turn"
    )
    create = AsyncMock(side_effect=[msg1, msg2])
    mock_anthropic.messages.create = create

    async def explode(_url: str) -> None:
        raise RuntimeError("ui handler dead")

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=explode,
        on_confirm=AsyncMock(return_value=True),
    )
    events = [ev async for ev in agent.run_turn("u@x", "list", [])]
    assert create.await_count == 1, (
        "spec requires the turn to abort: Claude must not be called a second time"
    )
    finals = [e for e in events if isinstance(e, Final)]
    assert finals and finals[-1].text == ""


async def test_qa_defect_4_confirm_timeout_emits_error_event(
    short_tmp: Path,
    populated_store: BindingStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("arcade_agent.daemon._CONFIRM_TIMEOUT", 0.2)

    script: list[dict[str, Any]] = [
        {"type": "tool_call", "name": "Gmail.SendEmail", "args": {}},
        {"type": "confirm", "tool": "Gmail.SendEmail", "args": {}},
        {"type": "final", "text": "done"},
    ]
    server, task = await _spawn_server(short_tmp, make_factory(script), populated_store)
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        await _send_line(
            writer,
            {"op": "prompt", "binding": "personal", "prompt": "send", "session_id": "S"},
        )
        evt = await _recv_line(reader)
        assert evt["event"] == "tool_call"
        evt = await _recv_line(reader)
        assert evt["event"] == "confirm"

        events_after: list[dict[str, Any]] = []
        for _ in range(5):
            try:
                events_after.append(await _recv_line(reader, timeout=2.0))
            except TimeoutError:
                break
        kinds = [e["event"] for e in events_after]
        assert "error" in kinds, f"expected an 'error' event after the timeout; got kinds={kinds}"
        err_msg = next(e["message"] for e in events_after if e["event"] == "error")
        assert "tim" in err_msg.lower() or "timeout" in err_msg.lower()

        writer.close()
        await writer.wait_closed()
    finally:
        await _shutdown(server, task)


async def test_qa_defect_5_daemon_history_is_evictable(
    short_tmp: Path,
    populated_store: BindingStore,
) -> None:
    """QA-DEFECT-5: idle session_ids must be evicted from _histories/_locks."""
    script: list[dict[str, Any]] = [{"type": "final", "text": "ok"}]
    server = DaemonServer(
        make_factory(script),
        populated_store,
        socket_path=short_tmp / "agent.sock",
        pidfile=short_tmp / "daemon.pid",
        session_ttl_seconds=0.05,
        eviction_interval_seconds=0.05,
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

    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        for i in range(20):
            await _send_line(
                writer,
                {"op": "prompt", "binding": "personal", "prompt": "p", "session_id": f"s{i}"},
            )
            evt = await _recv_line(reader)
            assert evt["event"] == "final"
        writer.close()
        await writer.wait_closed()

        await asyncio.sleep(0.5)
        assert len(server._histories) < 20, (
            "no eviction policy: histories grow unbounded with unique session_ids"
        )
        assert len(server._locks) < 20, (
            "no eviction policy: locks grow unbounded with unique session_ids"
        )
    finally:
        await _shutdown(server, task)
