"""Typer CLI for arcade-agent.

User-facing commands. Network access is limited to ``init``/``bind add``/
``bind reauth`` (which drive Arcade consent) and the daemon UDS for ``ask``
and ``chat``. Every command catches :class:`ArcadeAgentError` at the boundary
and prints a Rich-formatted error before exiting with status 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from arcade_agent import __version__, config
from arcade_agent.bindings import Binding, BindingStore
from arcade_agent.confirm import render_summary
from arcade_agent.errors import (
    ArcadeAgentError,
    BindingNotFound,
    DaemonAlreadyRunning,
    DaemonNotRunning,
)

app = typer.Typer(
    name="arcade-agent",
    help="Gmail + Calendar agent powered by Arcade.dev and Anthropic Claude.",
    no_args_is_help=True,
)
bind_app = typer.Typer(name="bind", no_args_is_help=True, help="Manage Arcade user bindings.")
daemon_app = typer.Typer(name="daemon", no_args_is_help=True, help="Control the background daemon.")
app.add_typer(bind_app, name="bind")
app.add_typer(daemon_app, name="daemon")


def _get_console(ctx: typer.Context | None = None) -> Console:
    if ctx is not None and isinstance(ctx.obj, dict) and "console" in ctx.obj:
        return ctx.obj["console"]
    return Console()


@app.callback()
def _main(ctx: typer.Context) -> None:
    """Load ``.env`` and stash a shared Rich console on the typer context."""
    load_dotenv()
    if ctx.obj is None:
        ctx.obj = {"console": Console()}
    elif "console" not in ctx.obj:
        ctx.obj["console"] = Console()


@app.command("version")
def version_cmd() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def _require_env(console: Console) -> None:
    missing = [k for k in ("ARCADE_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        console.print(
            f"[red]Missing required environment variables:[/red] {', '.join(missing)}\n"
            "Run [bold]arcade-agent init[/bold] to set them up."
        )
        raise typer.Exit(code=1)


def _build_arcade_client() -> Any:
    """Construct an :class:`ArcadeAgentClient` wrapped around ``AsyncArcade``."""
    import arcadepy

    from arcade_agent.arcade_client import ArcadeAgentClient

    arcade = arcadepy.AsyncArcade(api_key=os.environ["ARCADE_API_KEY"])
    return ArcadeAgentClient(arcade, config.ALL_TOOLS)


async def _drive_pre_authorize(
    user_id: str,
    console: Console,
) -> None:
    """Trigger the consent flow for every configured toolkit, opening URLs."""
    client = _build_arcade_client()
    pending = await client.pre_authorize_all(user_id)
    if not pending:
        console.print("[green]All toolkits already authorized.[/green]")
        return

    for entry in pending:
        console.print(
            Panel(
                f"[bold]{entry.provider_id}[/bold]\n{entry.url}",
                title="Authorize in your browser",
                border_style="cyan",
            )
        )
        opened = False
        try:
            opened = webbrowser.open(entry.url)
        except Exception:
            opened = False
        if not opened:
            console.print(
                "[yellow]Could not open browser. Visit the URL above "
                f"for {entry.provider_id}.[/yellow]"
            )

    for entry in pending:
        if entry.auth_id is None:
            console.print(f"[yellow]No auth_id for {entry.provider_id}; skipping wait.[/yellow]")
            continue
        with console.status(f"Waiting for {entry.provider_id} consent..."):
            await client.wait_for_completion(entry.auth_id)
        console.print(f"[green]{entry.provider_id} authorized.[/green]")


@app.command("init")
def init_cmd(
    ctx: typer.Context,
    force_env: bool = typer.Option(False, "--force-env", help="Recreate .env even if it exists."),
) -> None:
    """Interactive first-run: write .env, register a default binding, and consent."""
    console = _get_console(ctx)
    env_path = Path.cwd() / ".env"
    needed = ["ARCADE_API_KEY", "ANTHROPIC_API_KEY"]
    missing = [k for k in needed if not os.environ.get(k)]

    if missing or force_env:
        if env_path.exists() and not force_env:
            console.print(
                f"[yellow]{env_path} already exists; not overwriting "
                "(use --force-env to replace).[/yellow]"
            )
        else:
            lines: list[str] = []
            for key in needed:
                current = os.environ.get(key, "")
                if current and not force_env:
                    lines.append(f"{key}={current}")
                    continue
                value = typer.prompt(
                    f"{key} (leave blank to fill in later)",
                    default="",
                    hide_input=True,
                    show_default=False,
                )
                lines.append(f"{key}={value}")
                if value:
                    os.environ[key] = value
            try:
                env_path.write_text("\n".join(lines) + "\n")
                try:
                    os.chmod(env_path, 0o600)
                except OSError:
                    pass
                console.print(f"[green]Wrote {env_path} (mode 600).[/green]")
            except OSError as exc:
                console.print(f"[red]Could not write {env_path}: {exc}[/red]")
                raise typer.Exit(code=1) from exc

    _require_env(console)

    console.print(Panel("Bind a default Arcade user_id (email) now.", border_style="cyan"))
    name = typer.prompt("Binding name", default="personal")
    user_id = typer.prompt("Arcade user_id (your email)")

    try:
        _bind_add_impl(name=name, user_id=user_id, make_default=True, console=console)
    except ArcadeAgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _bind_add_impl(
    *,
    name: str,
    user_id: str,
    make_default: bool,
    console: Console,
) -> None:
    _require_env(console)

    store = BindingStore()
    store.add(Binding(name=name, user_id=user_id), make_default=make_default)
    console.print(f"[green]Saved binding {name!r} → {user_id}.[/green]")
    asyncio.run(_drive_pre_authorize(user_id, console))


@bind_app.command("add")
def bind_add(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    user_id: str = typer.Option(..., "--user-id", "-u", help="Arcade user_id (email)."),
    make_default: bool = typer.Option(False, "--default", help="Mark this binding as default."),
) -> None:
    """Create a binding and drive Arcade consent for every configured toolkit."""
    console = _get_console(ctx)
    try:
        _bind_add_impl(name=name, user_id=user_id, make_default=make_default, console=console)
    except ArcadeAgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@bind_app.command("list")
def bind_list(ctx: typer.Context) -> None:
    """List stored bindings (default marked with *)."""
    console = _get_console(ctx)
    store = BindingStore()
    try:
        bindings = store.list()
    except ArcadeAgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if not bindings:
        console.print(
            "[dim]No bindings yet. Try `arcade-agent bind add NAME --user-id EMAIL`.[/dim]"
        )
        return

    try:
        default_name = store.get_default().name
    except BindingNotFound:
        default_name = None

    table = Table(title="Arcade bindings")
    table.add_column("")
    table.add_column("name")
    table.add_column("user_id")
    for b in bindings:
        marker = "*" if b.name == default_name else ""
        table.add_row(marker, b.name, b.user_id)
    console.print(table)


@bind_app.command("remove")
def bind_remove(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Remove a stored binding."""
    console = _get_console(ctx)
    if not yes and not Confirm.ask(f"Remove binding {name!r}?", default=False, console=console):
        console.print("[dim]Aborted.[/dim]")
        return
    store = BindingStore()
    try:
        store.remove(name)
    except ArcadeAgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Removed binding {name!r}.[/green]")


@bind_app.command("set-default")
def bind_set_default(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Mark a binding as the default for ask/chat."""
    console = _get_console(ctx)
    store = BindingStore()
    try:
        store.set_default(name)
    except ArcadeAgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Default binding is now {name!r}.[/green]")


@bind_app.command("reauth")
def bind_reauth(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Re-run the Arcade consent flow for an existing binding."""
    console = _get_console(ctx)
    _require_env(console)
    store = BindingStore()
    try:
        binding = store.get(name)
    except ArcadeAgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    try:
        asyncio.run(_drive_pre_authorize(binding.user_id, console))
    except ArcadeAgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@daemon_app.command("start")
def daemon_start(
    ctx: typer.Context,
    foreground: bool = typer.Option(
        False, "--foreground", "-f", help="Run in the foreground (no detach)."
    ),
) -> None:
    """Start the background daemon."""
    console = _get_console(ctx)
    from arcade_agent.daemon import DaemonServer, is_running, start_detached

    _require_env(console)

    status, pid = _daemon_status_tuple()
    if status == "running":
        console.print(f"[yellow]Daemon already running (pid={pid}).[/yellow]")
        return
    if status == "stale_pidfile":
        console.print("[dim]Cleaning up stale pidfile...[/dim]")
        try:
            config.PIDFILE.unlink()
        except FileNotFoundError:
            pass

    if foreground:

        async def _run_foreground() -> None:
            import anthropic
            import arcadepy

            from arcade_agent.arcade_client import ArcadeAgentClient
            from arcade_agent.claude_agent import ClaudeAgent, OnAuthURL, OnConfirm

            arcade = arcadepy.AsyncArcade(api_key=os.environ["ARCADE_API_KEY"])
            anthropic_client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            arcade_client = ArcadeAgentClient(arcade, config.ALL_TOOLS)

            def factory(on_auth_url: OnAuthURL, on_confirm: OnConfirm) -> ClaudeAgent:
                return ClaudeAgent(
                    anthropic_client,
                    arcade_client,
                    model=config.DEFAULT_ANTHROPIC_MODEL,
                    on_auth_url=on_auth_url,
                    on_confirm=on_confirm,
                )

            server = DaemonServer(factory, BindingStore())
            console.print("[green]Daemon running in foreground. Ctrl-C to stop.[/green]")
            await server.start()

        try:
            asyncio.run(_run_foreground())
        except DaemonAlreadyRunning as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            return
        except KeyboardInterrupt:
            pass
        return

    log_path = config.CONFIG_DIR / "daemon.log"
    try:
        pid = start_detached(log_path)
    except DaemonAlreadyRunning as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    deadline = time.monotonic() + 5.0
    started = False
    while time.monotonic() < deadline:
        if is_running() is not None and config.SOCKET_PATH.exists():
            started = True
            break
        time.sleep(0.05)

    if not started:
        console.print(
            f"[yellow]Daemon launched (pid={pid}) but socket has not appeared yet. "
            f"Check {log_path}.[/yellow]"
        )
        return
    console.print(
        f"[green]Daemon started (pid={pid}).[/green] logs: {log_path}, socket: {config.SOCKET_PATH}"
    )


def _daemon_status_tuple() -> tuple[str, int | None]:
    """Compute the daemon status without touching the filesystem more than needed."""
    pidfile = config.PIDFILE
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


@daemon_app.command("stop")
def daemon_stop(ctx: typer.Context) -> None:
    """Stop the running daemon (SIGTERM, then SIGKILL after 5s)."""
    console = _get_console(ctx)
    from arcade_agent.daemon import stop_running

    status, pid = _daemon_status_tuple()
    if status == "not_running":
        console.print("[dim]Daemon not running.[/dim]")
        return

    try:
        stopped = asyncio.run(stop_running())
    except OSError as exc:
        console.print(f"[red]Failed to stop daemon: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if stopped:
        console.print(f"[green]Daemon stopped (pid={pid}).[/green]")
    else:
        console.print("[dim]Daemon was not running.[/dim]")


@daemon_app.command("status")
def daemon_status(ctx: typer.Context) -> None:
    """Print the daemon's current status."""
    console = _get_console(ctx)
    status, pid = _daemon_status_tuple()
    socket_exists = config.SOCKET_PATH.exists()
    log_path = config.CONFIG_DIR / "daemon.log"

    if status == "running":
        console.print(
            f"[green]running[/green] pid={pid}, socket={config.SOCKET_PATH} "
            f"(exists={socket_exists}), log={log_path}"
        )
    elif status == "stale_pidfile":
        pid_part = f"pid={pid}" if pid is not None else "pid=?"
        console.print(
            f"[yellow]stale pidfile[/yellow] {pid_part}, socket={config.SOCKET_PATH}, "
            f"log={log_path}"
        )
    else:
        console.print(f"[dim]not running. socket={config.SOCKET_PATH}, log={log_path}[/dim]")


@app.command("ask")
def ask_cmd(
    ctx: typer.Context,
    prompt: str = typer.Argument(...),
    binding: str | None = typer.Option(None, "--binding", "-b", help="Binding name to use."),
) -> None:
    """Send a single prompt to the daemon and stream the response."""
    console = _get_console(ctx)
    binding_name = _resolve_binding(binding, console)
    session_id = str(uuid.uuid4())
    try:
        asyncio.run(_send_and_stream(binding_name, prompt, session_id=session_id, console=console))
    except DaemonNotRunning:
        _print_daemon_not_running(console)
        raise typer.Exit(code=1) from None
    except ArcadeAgentError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command("chat")
def chat_cmd(
    ctx: typer.Context,
    binding: str | None = typer.Option(None, "--binding", "-b", help="Binding name to use."),
) -> None:
    """Start an interactive multi-turn chat session against the daemon."""
    console = _get_console(ctx)
    binding_name = _resolve_binding(binding, console)
    session_id = str(uuid.uuid4())
    console.print(
        f"[dim]chat session {session_id[:8]} (binding={binding_name}). Type /exit to quit.[/dim]"
    )

    while True:
        try:
            user_input = typer.prompt("> ", prompt_suffix="")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]goodbye[/dim]")
            return
        if user_input.strip().lower() in {"/exit", "/quit"}:
            console.print("[dim]goodbye[/dim]")
            return
        if not user_input.strip():
            continue
        try:
            asyncio.run(
                _send_and_stream(binding_name, user_input, session_id=session_id, console=console)
            )
        except DaemonNotRunning:
            _print_daemon_not_running(console)
            return
        except KeyboardInterrupt:
            console.print("\n[dim]interrupted[/dim]")
            continue
        except ArcadeAgentError as exc:
            console.print(f"[red]{exc}[/red]")
            continue


def _resolve_binding(binding: str | None, console: Console) -> str:
    store = BindingStore()
    if binding is None:
        try:
            return store.get_default().name
        except BindingNotFound as exc:
            console.print(
                "[red]No default binding. Use --binding NAME or run "
                "`arcade-agent bind set-default NAME`.[/red]"
            )
            raise typer.Exit(code=1) from exc
    try:
        return store.get(binding).name
    except BindingNotFound as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _print_daemon_not_running(console: Console) -> None:
    console.print(
        "[red]Daemon not running.[/red] Start it with [bold]arcade-agent daemon start[/bold]."
    )


def _encode_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode("utf-8")


async def _send_and_stream(
    binding_name: str,
    prompt: str,
    *,
    session_id: str,
    console: Console,
) -> None:
    """Open the daemon UDS, send a prompt, render events until ``final``/``error``."""
    try:
        reader, writer = await asyncio.open_unix_connection(str(config.SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise DaemonNotRunning(str(exc)) from exc
    except OSError as exc:
        raise DaemonNotRunning(str(exc)) from exc

    try:
        writer.write(
            _encode_line(
                {
                    "op": "prompt",
                    "binding": binding_name,
                    "prompt": prompt,
                    "session_id": session_id,
                }
            )
        )
        await writer.drain()

        final_text = ""
        while True:
            line = await reader.readline()
            if not line:
                console.print("[red]Daemon closed the connection.[/red]")
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                console.print(f"[red]Malformed event from daemon: {line!r}[/red]")
                continue

            kind = event.get("event")
            if kind == "text":
                delta = event.get("delta", "")
                console.print(delta, end="", soft_wrap=True)
            elif kind == "tool_call":
                name = event.get("name", "?")
                args = event.get("args", {})
                console.print(
                    Panel(
                        json.dumps(args, indent=2, default=str),
                        title=f"tool_call: {name}",
                        border_style="cyan",
                    )
                )
            elif kind == "tool_result":
                name = event.get("name", "?")
                ok = event.get("ok", False)
                summary = event.get("summary", "")
                style = "green" if ok else "red"
                tag = "ok" if ok else "fail"
                console.print(f"[{style}]tool_result {tag}[/{style}] {name}: {summary}")
            elif kind == "auth_url":
                url = event.get("url", "")
                console.print(Panel(url, title="Authorize in browser", border_style="yellow"))
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            elif kind == "confirm":
                tool = event.get("tool", "?")
                args = event.get("args", {}) or {}
                summary = render_summary(tool, args)
                approved = Confirm.ask(f"\n{summary}\nApprove?", default=False, console=console)
                writer.write(
                    _encode_line(
                        {
                            "op": "confirm_ack",
                            "session_id": session_id,
                            "approved": bool(approved),
                        }
                    )
                )
                await writer.drain()
            elif kind == "final":
                final_text = event.get("text", "") or ""
                if final_text:
                    console.print()
                    console.print(Panel(final_text, title="assistant", border_style="green"))
                else:
                    console.print()
                return
            elif kind == "error":
                message = event.get("message", "?")
                console.print()
                console.print(f"[red]error:[/red] {message}")
                return
            else:
                console.print(f"[yellow]unknown event:[/yellow] {event}")
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


__all__ = [
    "app",
    "bind_app",
    "daemon_app",
]
