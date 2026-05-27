"""Background daemon: Unix-socket listener plus a Claude tool-use loop driver.

This module owns the daemon's lifecycle (pidfile, double-fork detach, signal
handling) and its wire protocol surface (NDJSON over a Unix domain socket as
described in `docs/IMPLEMENTATION_SPEC.md` §6).

External dependencies — a :class:`ClaudeAgent` factory and a
:class:`BindingStore` — are injected so that tests can swap a scripted fake
agent in via :class:`DaemonServer`'s constructor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcade_agent import config
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
from arcade_agent.errors import (
    BindingNotFound,
    ConfigError,
    DaemonAlreadyRunning,
)

if TYPE_CHECKING:
    from arcade_agent.bindings import BindingStore
    from arcade_agent.claude_agent import ClaudeAgent, Event

logger = logging.getLogger(__name__)

AgentFactory = Callable[[OnAuthURL, OnConfirm], "ClaudeAgent"]

_CONFIRM_TIMEOUT = 300.0
_SHUTDOWN_DRAIN_SECONDS = 5.0
_DEFAULT_SESSION_TTL_SECONDS = 1800.0
_DEFAULT_EVICTION_INTERVAL_SECONDS = 60.0


def _encode_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def is_running(pidfile: Path | None = None) -> int | None:
    """Return the pid if ``pidfile`` points at a live process, else ``None``.

    Never deletes a stale pidfile — :meth:`DaemonServer.start` handles cleanup.
    """
    pidfile = pidfile if pidfile is not None else config.PIDFILE
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _status_impl(pidfile: Path) -> tuple[str, int | None]:
    if not pidfile.exists():
        return ("not_running", None)
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return ("stale_pidfile", None)
    try:
        os.kill(pid, 0)
    except OSError:
        return ("stale_pidfile", pid)
    return ("running", pid)


class DaemonServer:
    """Unix-socket server that proxies prompts through ``ClaudeAgent`` and back.

    The class owns no global state. Construct it with an ``agent_factory``
    callable (one ClaudeAgent per prompt, wired with per-connection callbacks)
    and a :class:`BindingStore`; tests pass a fake factory + an in-memory store.
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        binding_store: BindingStore,
        *,
        socket_path: Path | None = None,
        pidfile: Path | None = None,
        session_ttl_seconds: float = _DEFAULT_SESSION_TTL_SECONDS,
        eviction_interval_seconds: float = _DEFAULT_EVICTION_INTERVAL_SECONDS,
    ) -> None:
        self._agent_factory = agent_factory
        self._bindings = binding_store
        self._socket_path: Path = socket_path if socket_path is not None else config.SOCKET_PATH
        self._pidfile: Path = pidfile if pidfile is not None else config.PIDFILE
        self._server: asyncio.AbstractServer | None = None
        self._histories: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_active: dict[str, float] = {}
        self._pending_confirms: dict[str, asyncio.Future[bool]] = {}
        self._stop_fut: asyncio.Future[None] | None = None
        self._stopped: bool = False
        self._session_ttl_seconds: float = session_ttl_seconds
        self._eviction_interval_seconds: float = eviction_interval_seconds
        self._eviction_task: asyncio.Task[None] | None = None

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def pidfile(self) -> Path:
        return self._pidfile

    def status(self) -> tuple[str, int | None]:
        """Return ``("running"|"stale_pidfile"|"not_running", pid|None)``."""
        return _status_impl(self._pidfile)

    async def start(self) -> None:
        """Bind the socket, write the pidfile, and serve until stopped.

        Raises :class:`DaemonAlreadyRunning` if another daemon already holds
        the pidfile, or :class:`ConfigError` on a non-POSIX platform.
        """
        if sys.platform == "win32":
            raise ConfigError("Unix sockets are required; this platform is not supported")

        self._pidfile.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.parent != self._pidfile.parent:
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        status, pid = _status_impl(self._pidfile)
        if status == "running":
            assert pid is not None
            raise DaemonAlreadyRunning(pid)
        if status == "stale_pidfile":
            with contextlib.suppress(FileNotFoundError):
                self._pidfile.unlink()

        if self._socket_path.exists() or self._socket_path.is_symlink():
            with contextlib.suppress(FileNotFoundError):
                self._socket_path.unlink()

        self._pidfile.write_text(f"{os.getpid()}\n")
        try:
            os.chmod(self._pidfile, 0o600)
        except OSError:
            pass

        server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self._socket_path)
        )
        try:
            os.chmod(self._socket_path, 0o600)
        except OSError:
            pass
        self._server = server
        logger.info("daemon listening on %s (pid=%s)", self._socket_path, os.getpid())

        loop = asyncio.get_running_loop()
        self._stop_fut = loop.create_future()
        self._eviction_task = asyncio.create_task(self._eviction_loop())

        def _on_signal() -> None:
            logger.info("daemon received shutdown signal")
            if self._stop_fut is not None and not self._stop_fut.done():
                self._stop_fut.set_result(None)

        installed: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _on_signal)
                installed.append(sig)
            except (NotImplementedError, ValueError):
                pass

        try:
            await self._stop_fut
        finally:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.remove_signal_handler(sig)
            await self.stop()

    async def stop(self) -> None:
        """Cleanly shut the running server down and remove socket + pidfile."""
        if self._stopped:
            return
        self._stopped = True

        if self._stop_fut is not None and not self._stop_fut.done():
            self._stop_fut.set_result(None)

        for fut in list(self._pending_confirms.values()):
            if not fut.done():
                fut.set_result(False)
        self._pending_confirms.clear()

        if self._eviction_task is not None:
            self._eviction_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._eviction_task
            self._eviction_task = None

        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=_SHUTDOWN_DRAIN_SECONDS)
            except TimeoutError:
                logger.warning("server.wait_closed timed out; forcing shutdown")
            except Exception:
                logger.exception("error while closing server")
            self._server = None

        for path in (self._socket_path, self._pidfile):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    async def _eviction_loop(self) -> None:
        """Periodically drop session state for sessions idle past the TTL."""
        try:
            while True:
                await asyncio.sleep(self._eviction_interval_seconds)
                self._evict_stale_sessions()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("eviction loop crashed")

    def _evict_stale_sessions(self) -> None:
        now = time.monotonic()
        cutoff = now - self._session_ttl_seconds
        stale = [
            sid
            for sid, last in self._last_active.items()
            if last < cutoff and sid not in self._pending_confirms
        ]
        for sid in stale:
            self._histories.pop(sid, None)
            self._locks.pop(sid, None)
            self._last_active.pop(sid, None)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        write_lock = asyncio.Lock()
        active_tasks: list[asyncio.Task[None]] = []

        async def send(payload: dict[str, Any]) -> None:
            async with write_lock:
                try:
                    writer.write(_encode_line(payload))
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError):
                    logger.debug("client disconnected mid-write")
                except Exception:
                    logger.exception("failed to write to client")

        try:
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionResetError, BrokenPipeError):
                    break
                if not line:
                    break

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    await send({"event": "error", "message": "malformed JSON"})
                    continue

                if not isinstance(msg, dict):
                    await send({"event": "error", "message": "expected JSON object"})
                    continue

                op = msg.get("op")
                if not isinstance(op, str):
                    await send({"event": "error", "message": "missing 'op'"})
                    continue

                session_hint = msg.get("session_id")
                if isinstance(session_hint, str) and session_hint:
                    self._last_active[session_hint] = time.monotonic()

                if op == "prompt":
                    task = asyncio.create_task(self._run_prompt(msg, send))
                    active_tasks.append(task)
                    active_tasks[:] = [t for t in active_tasks if not t.done()]
                elif op == "confirm_ack":
                    await self._handle_confirm_ack(msg, send)
                else:
                    await send({"event": "error", "message": f"unknown op: {op!r}"})
        finally:
            for task in active_tasks:
                task.cancel()
            for task in active_tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _handle_confirm_ack(
        self,
        msg: dict[str, Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        session_id = msg.get("session_id")
        approved = msg.get("approved")
        if not isinstance(session_id, str) or not session_id:
            await send({"event": "error", "message": "confirm_ack missing 'session_id'"})
            return
        if not isinstance(approved, bool):
            await send({"event": "error", "message": "confirm_ack missing 'approved'"})
            return
        fut = self._pending_confirms.get(session_id)
        if fut is None or fut.done():
            await send({"event": "error", "message": "no pending confirm for session"})
            return
        fut.set_result(approved)

    async def _run_prompt(
        self,
        msg: dict[str, Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        session_id = msg.get("session_id")
        binding_name = msg.get("binding")
        prompt = msg.get("prompt")

        if not isinstance(session_id, str) or not session_id:
            await send({"event": "error", "message": "prompt missing 'session_id'"})
            return
        if not isinstance(binding_name, str) or not binding_name:
            await send({"event": "error", "message": "prompt missing 'binding'"})
            return
        if not isinstance(prompt, str):
            await send({"event": "error", "message": "prompt missing 'prompt'"})
            return

        try:
            binding = self._bindings.get(binding_name)
        except BindingNotFound:
            await send({"event": "error", "message": f"unknown binding {binding_name!r}"})
            return

        history = self._histories.setdefault(session_id, [])
        lock = self._locks.setdefault(session_id, asyncio.Lock())

        async def on_auth_url(url: str) -> None:
            await send({"event": "auth_url", "url": url})

        async def on_confirm(tool_name: str, args: dict[str, Any]) -> bool:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bool] = loop.create_future()
            self._pending_confirms[session_id] = fut
            try:
                await send(
                    {
                        "event": "confirm",
                        "tool": tool_name,
                        "args": args,
                        "session_id": session_id,
                    }
                )
                try:
                    return await asyncio.wait_for(fut, timeout=_CONFIRM_TIMEOUT)
                except TimeoutError:
                    logger.warning("confirm timeout for session %s", session_id)
                    await send({"event": "error", "message": "confirmation timed out"})
                    return False
            finally:
                self._pending_confirms.pop(session_id, None)

        try:
            agent = self._agent_factory(on_auth_url, on_confirm)
        except Exception as exc:
            logger.exception("agent factory failed")
            await send({"event": "error", "message": f"agent setup failed: {exc}"})
            return

        async with lock:
            try:
                async for event in agent.run_turn(binding.user_id, prompt, history):
                    payload = _event_to_wire(event, session_id)
                    if payload is None:
                        continue
                    await send(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("run_turn crashed")
                await send({"event": "error", "message": f"agent crashed: {exc}"})
                await send({"event": "final", "text": ""})


def _event_to_wire(event: Event, session_id: str) -> dict[str, Any] | None:
    """Map a ClaudeAgent event to a wire-protocol JSON dict (or skip)."""
    if isinstance(event, TextDelta):
        return {"event": "text", "delta": event.text}
    if isinstance(event, ToolCallStarted):
        return {"event": "tool_call", "name": event.name, "args": event.args}
    if isinstance(event, ToolCallResult):
        return {
            "event": "tool_result",
            "name": event.name,
            "ok": event.ok,
            "summary": event.summary,
        }
    if isinstance(event, AuthURLRequested):
        # The wire `auth_url` event is emitted by the `on_auth_url` callback
        # (called by the agent before yielding this dataclass). Skipping the
        # yield avoids a duplicate event on the socket; the AuthURLRequested
        # dataclass stays useful as an in-process observability signal.
        return None
    if isinstance(event, ConfirmRequested):
        # Same rationale as AuthURLRequested: the wire `confirm` event is
        # emitted by the `on_confirm` callback (which also awaits the
        # matching `confirm_ack`). Skipping prevents a duplicate.
        return None
    if isinstance(event, Final):
        return {"event": "final", "text": event.text}
    if isinstance(event, ErrorEvent):
        return {"event": "error", "message": event.message}
    return None


def start_detached(log_path: Path | None = None) -> int:
    """Double-fork into a detached daemon process.

    Returns the grandchild PID to the original parent. The grandchild starts
    a fresh asyncio loop and runs :meth:`DaemonServer.start`. On non-POSIX
    platforms raises :class:`RuntimeError`.
    """
    if sys.platform == "win32" or not hasattr(os, "fork"):
        raise RuntimeError("detach not supported on this platform")
    if log_path is None:
        log_path = config.CONFIG_DIR / "daemon.log"

    config.ensure_config_dir()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(w_fd)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        with os.fdopen(r_fd, "rb") as r:
            data = r.read(32)
        try:
            return int(data.decode().strip())
        except (ValueError, UnicodeDecodeError):
            return -1

    os.close(r_fd)

    try:
        os.setsid()
        os.umask(0o077)
        os.chdir("/")

        pid2 = os.fork()
        if pid2 > 0:
            try:
                os.write(w_fd, f"{pid2}\n".encode())
            finally:
                os.close(w_fd)
            os._exit(0)
        os.close(w_fd)
    except BaseException:
        os._exit(1)

    try:
        with open(os.devnull, "rb") as devnull:
            os.dup2(devnull.fileno(), 0)
        log_fd = os.open(
            str(log_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
        finally:
            os.close(log_fd)
    except BaseException:
        os._exit(1)

    try:
        _daemon_main(log_path)
    except BaseException:
        logger.exception("daemon main crashed")
        os._exit(1)
    os._exit(0)


def _daemon_main(log_path: Path) -> None:
    """Entry point for the grandchild process spawned by :func:`start_detached`."""
    level_name = os.environ.get("ARCADE_AGENT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        filename=str(log_path),
        filemode="a",
    )

    import anthropic
    import arcadepy

    from arcade_agent.arcade_client import ArcadeAgentClient
    from arcade_agent.bindings import BindingStore
    from arcade_agent.claude_agent import ClaudeAgent

    bindings = BindingStore()
    arcade = arcadepy.AsyncArcade()
    anthropic_client = anthropic.AsyncAnthropic()
    arcade_client = ArcadeAgentClient(arcade, config.ALL_TOOLS)

    def factory(on_auth_url: OnAuthURL, on_confirm: OnConfirm) -> ClaudeAgent:
        return ClaudeAgent(
            anthropic_client,
            arcade_client,
            model=config.DEFAULT_ANTHROPIC_MODEL,
            on_auth_url=on_auth_url,
            on_confirm=on_confirm,
        )

    server = DaemonServer(factory, bindings)
    asyncio.run(server.start())


async def stop_running(
    *,
    pidfile: Path | None = None,
    socket_path: Path | None = None,
    timeout: float = 5.0,
) -> bool:
    """Send SIGTERM to a running daemon and clean up its socket + pidfile.

    Returns True if a daemon was running and was stopped, False otherwise.
    """
    pidfile = pidfile if pidfile is not None else config.PIDFILE
    socket_path = socket_path if socket_path is not None else config.SOCKET_PATH

    pid = is_running(pidfile)
    if pid is None:
        for path in (pidfile, socket_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        await asyncio.sleep(0.1)
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    for path in (pidfile, socket_path):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    return True
