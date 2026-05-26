# arcade-agent

A portable demo Python agent that runs as a local background daemon and uses
[Arcade.dev](https://arcade.dev)'s hosted Gmail + Google Calendar tools, driven by
Anthropic Claude.

> Status: WIP. Scaffolding in place; module implementations land next.

## Why it exists

To prove out three things together:
1. Anthropic Claude doing tool-use against
2. Arcade-hosted Gmail + Calendar tools, with
3. Per-user Google OAuth handled entirely by Arcade (no local Google client, no
   refresh tokens on disk, no OS keychain).

## Quickstart (once implementation lands)

```bash
# 1. Install
pipx install arcade-agent          # or: uv pip install -e .

# 2. Configure secrets
cp .env.example .env
# edit .env: ARCADE_API_KEY, ANTHROPIC_API_KEY

# 3. First-run wizard (creates a binding, opens browser for Google consent via Arcade)
arcade-agent init

# 4. Start the daemon
arcade-agent daemon start

# 5. Talk to it
arcade-agent ask "list my last 3 emails"
arcade-agent chat
```

## Architecture

See [arcade-agent plan](../../.cursor/plans/gmail-gcal-agent_e075e8db.plan.md) for
the full design. Short version:

- **CLI** (`arcade-agent`) talks to a **daemon** over a Unix socket.
- **Daemon** runs an Anthropic Claude tool-use loop. Tools are pulled from
  Arcade Cloud via `arcadepy` (`Gmail.*`, `GoogleCalendar.*`).
- **Auth**: Arcade owns the Google OAuth app and stores refresh tokens
  server-side. The only local secret is the `ARCADE_API_KEY` in `.env`.
- **Bindings**: named `user_id` mappings in `~/.arcade-agent/bindings.toml`.
- **Confirmation gate**: ON by default for `Gmail.SendEmail`,
  `GoogleCalendar.DeleteEvent`, `GoogleCalendar.UpdateEvent`.

## Project layout

```
src/arcade_agent/
  cli.py            Typer CLI
  config.py         paths + toolkit list
  arcade_client.py  arcadepy wrapper (schemas, authorize, execute)
  bindings.py       name -> arcade user_id registry
  daemon.py         UDS server + lifecycle
  claude_agent.py   Anthropic tool-use loop
  confirm.py        confirmation-gate helpers
```
