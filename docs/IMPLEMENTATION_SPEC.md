# `arcade-agent` — Implementation Spec

> **Audience.** A developer subagent implementing every module from scratch on top of the
> existing scaffold. This document is normative: do not invent additional modules, do not
> change file paths, do not pull in extra dependencies, and do not deviate from the public
> APIs below without first updating this spec.
>
> **Source of truth.** `/Users/taka/.cursor/plans/gmail-gcal-agent_e075e8db.plan.md`. This
> document refines that plan into concrete signatures. If anything contradicts the plan,
> the plan wins and this doc must be updated.

---

## Table of contents

1. [Project invariants](#1-project-invariants)
2. [Repository layout (final)](#2-repository-layout-final)
3. [External SDK facts (verified)](#3-external-sdk-facts-verified)
   - 3.1 [`arcadepy.AsyncArcade`](#31-arcadepyasyncarcade)
   - 3.2 [`anthropic.AsyncAnthropic`](#32-anthropicasyncanthropic)
4. [Cross-cutting requirements](#4-cross-cutting-requirements)
5. [Module specs (dependency order)](#5-module-specs-dependency-order)
   - 5.1 [`errors.py`](#51-errorspy)
   - 5.2 [`bindings.py`](#52-bindingspy)
   - 5.3 [`arcade_client.py`](#53-arcade_clientpy)
   - 5.4 [`confirm.py`](#54-confirmpy)
   - 5.5 [`claude_agent.py`](#55-claude_agentpy)
   - 5.6 [`daemon.py`](#56-daemonpy)
   - 5.7 [`cli.py` (replaces current stub)](#57-clipy-replaces-current-stub)
6. [Wire protocol — daemon UDS](#6-wire-protocol--daemon-uds)
7. [Testing strategy](#7-testing-strategy)
   - 7.1 [Per-module unit tests](#71-per-module-unit-tests)
   - 7.2 [Daemon integration test](#72-daemon-integration-test)
   - 7.3 [`tests/conftest.py` outline](#73-testsconftestpy-outline)
   - 7.4 [Live smoke probe — `scripts/probe.py`](#74-live-smoke-probe--scriptsprobepy)
8. [Implementation order & acceptance gates](#8-implementation-order--acceptance-gates)

---

## 1. Project invariants

These are **hard constraints**. Implementation MUST NOT change them.

| # | Invariant |
|---|---|
| I1 | Python `>=3.11`. `async` where it is natural (network, sockets); plain functions elsewhere. |
| I2 | LLM brain is Anthropic Claude via the `anthropic` SDK using native tool-use. Model name comes from `config.DEFAULT_ANTHROPIC_MODEL`. |
| I3 | All tools come from Arcade Cloud via `arcadepy.AsyncArcade`. The tool set exposed to Claude is exactly `config.ALL_TOOLS`. |
| I4 | Daemon listens on `config.SOCKET_PATH` (Unix domain socket). Newline-delimited JSON (NDJSON), one JSON object per line, bidirectional. No HTTP, no TCP. |
| I5 | Confirmation gate is **on by default** for any tool name in `config.DESTRUCTIVE_TOOLS`. |
| I6 | Bindings persist in `config.BINDINGS_FILE` (`~/.arcade-agent/bindings.toml`). Secrets are never written to that file. |
| I7 | Secrets `ARCADE_API_KEY` and `ANTHROPIC_API_KEY` are read from the environment, loaded via `python-dotenv` from `.env` if present. |
| I8 | Session state is **in-memory only**. Lost on daemon restart. |
| I9 | No audit log. No OS keyring. No Google API libraries. No MCP server in this repo. |
| I10 | No global mutable state. External dependencies (`AsyncArcade`, `AsyncAnthropic`, `BindingStore`) are injected via constructors. |
| I11 | Every Python file starts with `from __future__ import annotations`. Full type hints on every public callable. |
| I12 | Logging via the stdlib `logging` module inside `daemon.py` and `claude_agent.py`. CLI uses `rich` for user-facing output. **No `print()` inside daemon/agent code.** |
| I13 | Errors raise typed exceptions from `errors.py`. SDK exceptions (HTTP, validation, etc.) are caught at module boundaries and re-raised as typed exceptions where it improves the caller's error model. |

---

## 2. Repository layout (final)

```
arcade-agent/
├── pyproject.toml                # exists, do not touch beyond adding deps
├── README.md                     # exists
├── .env.example                  # add (ARCADE_API_KEY=, ANTHROPIC_API_KEY=)
├── docs/
│   └── IMPLEMENTATION_SPEC.md    # this file
├── scripts/
│   └── probe.py                  # live smoke test (see §7.4)
├── src/arcade_agent/
│   ├── __init__.py               # exists; just __version__
│   ├── config.py                 # exists; KEEP AS-IS
│   ├── errors.py                 # NEW
│   ├── bindings.py               # implement
│   ├── arcade_client.py          # implement
│   ├── confirm.py                # implement
│   ├── claude_agent.py           # implement
│   ├── daemon.py                 # implement
│   └── cli.py                    # REPLACE stub
└── tests/
    ├── conftest.py               # NEW (fixtures)
    ├── test_bindings.py
    ├── test_arcade_client.py
    ├── test_confirm.py
    ├── test_claude_agent.py
    ├── test_daemon.py            # integration: real UDS, mocked agent
    └── test_cli.py               # uses typer.testing.CliRunner
```

Disallowed: adding new top-level modules, splitting modules across files, or adding submodules under `src/arcade_agent/`. If a helper grows, keep it inside the existing module.

---

## 3. External SDK facts (verified)

The signatures below were verified against the libraries installed in
`/Users/taka/petprojects/arcade-agent/.venv`. Treat them as canonical for this build.

### 3.1 `arcadepy.AsyncArcade`

**Constructor:**

```python
AsyncArcade(
    *,
    api_key: str | None = None,           # falls back to ARCADE_API_KEY env var
    base_url: str | httpx.URL | None = None,
    timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    max_retries: int = 2,
    default_headers: Mapping[str, str] | None = None,
    default_query: Mapping[str, object] | None = None,
    http_client: httpx.AsyncClient | None = None,
)
```

**`client.tools` (AsyncToolsResource):**

```python
async def list(
    *,
    include_format: list[Literal["arcade", "openai", "anthropic"]] | Omit = omit,
    limit: int | Omit = omit,           # default 25, max 100
    offset: int | Omit = omit,
    toolkit: str | Omit = omit,         # e.g. "Gmail" or "GoogleCalendar"
    user_id: str | Omit = omit,
    ...
) -> AsyncPaginator[ToolDefinition, AsyncOffsetPage[ToolDefinition]]

async def authorize(
    *,
    tool_name: str,                     # fully-qualified, e.g. "Gmail.SendEmail"
    next_uri: str | Omit = omit,
    tool_version: str | Omit = omit,
    user_id: str | Omit = omit,
    ...
) -> AuthorizationResponse

async def execute(
    *,
    tool_name: str,
    input: dict[str, object] | Omit = omit,
    user_id: str | Omit = omit,
    tool_version: str | Omit = omit,
    run_at: str | Omit = omit,
    include_error_stacktrace: bool | Omit = omit,
    ...
) -> ExecuteToolResponse
```

**`client.auth` (AsyncAuthResource):**

```python
async def wait_for_completion(
    auth_response_or_id: AuthorizationResponse | str,
) -> AuthorizationResponse

async def status(*, id: str, wait: int | Omit = omit, ...) -> AuthorizationResponse

# Note: client.auth.start is sync in this SDK version; do not use it from async code
# unless wrapped in asyncio.to_thread. Prefer client.tools.authorize for our flow.
```

**`ToolDefinition`** (pydantic model):

```
fully_qualified_name: str         # e.g. "Gmail.SendEmail@1.0.0"
qualified_name: str               # e.g. "Gmail.SendEmail"   <-- use this as the canonical Arcade tool_name
name: str                         # e.g. "SendEmail"
toolkit: Toolkit{name, description, version}
description: str | None
input: Input{ parameters: list[InputParameter] | None }
output: Output | None
requirements: Requirements{
    authorization: RequirementsAuthorization{
        provider_id: str | None,            # e.g. "google"
        provider_type: str | None,          # e.g. "oauth2"
        status: Literal["active", "inactive"] | None,
        token_status: Literal["not_started", "pending", "completed", "failed"] | None,
    } | None,
    met: bool | None,
    secrets: list[RequirementsSecret] | None,
} | None
formatted_schema: dict[str, object] | None   # populated when include_format is passed
```

**`ExecuteToolResponse`:**

```
id: str | None
execution_id: str | None
status: str | None
success: bool | None
duration: float | None
finished_at: str | None
run_at: str | None
output: Output{
    value: object | None,                    # the actual tool result on success
    error: OutputError{                      # populated on failure
        kind: Literal[
            "TOOLKIT_LOAD_FAILED", "TOOL_DEFINITION_BAD_DEFINITION",
            "TOOL_DEFINITION_BAD_INPUT_SCHEMA", "TOOL_DEFINITION_BAD_OUTPUT_SCHEMA",
            "TOOL_REQUIREMENTS_NOT_MET",     # <-- the "auth required" case
            "TOOL_RUNTIME_BAD_INPUT_VALUE", "TOOL_RUNTIME_BAD_OUTPUT_VALUE",
            "TOOL_RUNTIME_RETRY", "TOOL_RUNTIME_CONTEXT_REQUIRED",
            "TOOL_RUNTIME_FATAL",
            "UPSTREAM_RUNTIME_BAD_REQUEST", "UPSTREAM_RUNTIME_AUTH_ERROR",
            "UPSTREAM_RUNTIME_NOT_FOUND", "UPSTREAM_RUNTIME_VALIDATION_ERROR",
            "UPSTREAM_RUNTIME_RATE_LIMIT", "UPSTREAM_RUNTIME_SERVER_ERROR",
            "UPSTREAM_RUNTIME_UNMAPPED", "UNKNOWN",
        ],
        message: str,
        can_retry: bool,
        additional_prompt_content: str | None,
        developer_message: str | None,
        retry_after_ms: int | None,
        status_code: int | None,
        ...
    } | None,
    authorization: AuthorizationResponse | None,   # populated when auth needed
    logs: list[OutputLog] | None,
} | None
```

**`AuthorizationResponse`:**

```
id: str | None
status: Literal["not_started", "pending", "completed", "failed"] | None
url: str | None                       # consent URL when pending
provider_id: str | None
scopes: list[str] | None
context: AuthorizationContext{ token, user_info } | None
user_id: str | None
```

**Auth-required detection rule** (canonical for this codebase):

> A call to `client.tools.execute(...)` is treated as **auth required** when
> `response.success is False` **and** either `response.output.authorization` is set with a
> non-`completed` status, **or** `response.output.error.kind == "TOOL_REQUIREMENTS_NOT_MET"`.
> The consent URL is `response.output.authorization.url` if present; otherwise the
> client must call `client.tools.authorize(tool_name=..., user_id=...)` to obtain one.

### 3.2 `anthropic.AsyncAnthropic`

**Constructor:**

```python
AsyncAnthropic(*, api_key: str | None = None, ...)   # ANTHROPIC_API_KEY env fallback
```

**`client.messages.create` (the only entry point we use):**

```python
async def create(
    *,
    model: str,
    max_tokens: int,
    messages: list[MessageParam],
    tools: list[ToolParam] | Omit = omit,
    system: str | list[TextBlockParam] | Omit = omit,
    tool_choice: ToolChoiceParam | Omit = omit,
    temperature: float | Omit = omit,
    ...
) -> Message
```

Where `ToolParam` is `{"name": str, "description"?: str, "input_schema": dict}`.

**`Message`:**

```
id: str
role: Literal["assistant"]
content: list[ContentBlock]      # TextBlock | ToolUseBlock | ... (we ignore the rest)
stop_reason: Literal["end_turn", "max_tokens", "stop_sequence", "tool_use",
                     "pause_turn", "refusal"] | None
stop_sequence: str | None
usage: Usage
```

**Blocks we care about:**

```
TextBlock     { type: "text", text: str }
ToolUseBlock  { type: "tool_use", id: str, name: str, input: dict[str, object] }
```

**Sending a tool result back** (next user turn):

```python
{
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "<the tool_use block id>",
            "content": "<stringified result or list[ContentBlockParam]>",
            "is_error": False,                       # set True on failure
        }
    ],
}
```

> **Tool-name constraint.** Anthropic requires tool names to match
> `^[a-zA-Z0-9_-]{1,64}$`. Arcade tool names contain `.` (e.g. `Gmail.SendEmail`), so
> we MUST normalize dot→underscore for Anthropic and keep a reverse map. See §5.3.

---

## 4. Cross-cutting requirements

- **`from __future__ import annotations`** at the top of every Python file in the package.
- **Type hints** on every public callable (parameters and return). Use `collections.abc`
  for `Awaitable`, `AsyncIterator`, etc.
- **Async runtime:** `asyncio` only. No `trio`, no `anyio`. The daemon uses
  `asyncio.start_unix_server`. CLI sync entry points create a single event loop via
  `asyncio.run`.
- **Logging:** stdlib `logging`. Module-level loggers `logging.getLogger(__name__)`.
  The daemon's `start_detached` MUST configure the root logger to write to
  `~/.arcade-agent/daemon.log` (rotating not required for the demo; plain `FileHandler`).
  Default level `INFO`. Level overridable via `ARCADE_AGENT_LOG_LEVEL` env var.
- **No `print()`** anywhere in `daemon.py`, `claude_agent.py`, `arcade_client.py`,
  `bindings.py`, or `confirm.py`. CLI uses `rich.console.Console` for output.
- **No global mutable state.** Everything is constructed at the edge (CLI / daemon
  `main`) and passed down via constructors. The only acceptable module-level state is
  truly-constant data (`config.py` values).
- **Concurrency:** the daemon allows multiple concurrent client connections. Each
  connection runs in its own task and has its own session id. Per-session history
  access is single-tasked (one prompt per session at a time); enforce with an
  `asyncio.Lock` keyed by `session_id`.
- **Timeouts:** wrap `client.tools.execute` and `client.tools.authorize` in
  `asyncio.wait_for(..., timeout=120)`. `client.auth.wait_for_completion` gets a
  longer 300 s timeout (human consent). Timeouts raise `ArcadeToolError`.
- **Retries:** rely on `arcadepy`'s built-in `max_retries=2`. Do not add another retry
  layer. If `OutputError.can_retry is True` and `retry_after_ms` is set, the
  `ArcadeAgentClient.execute` method MAY do one in-process retry after sleeping —
  but the default in v1 is **no extra retry**; surface the error.
- **JSON serialization:** all daemon I/O uses `json.dumps(obj, separators=(",", ":"),
  default=str)` and `json.loads` per line. Always terminate output lines with `\n`.

---

## 5. Module specs (dependency order)

Implement modules in this order. Each module's tests should pass before moving on.

### 5.1 `errors.py`

New file. Pure exception hierarchy. No dependencies on anything but stdlib.

```python
from __future__ import annotations


class ArcadeAgentError(Exception):
    """Base class for every typed error raised by this package."""


class ConfigError(ArcadeAgentError):
    """Missing/invalid configuration (env var, malformed bindings file, etc.)."""


class BindingNotFound(ArcadeAgentError):
    def __init__(self, name: str) -> None:
        super().__init__(f"binding not found: {name!r}")
        self.name = name


class BindingExists(ArcadeAgentError):
    def __init__(self, name: str) -> None:
        super().__init__(f"binding already exists: {name!r}")
        self.name = name


class ArcadeAuthRequired(ArcadeAgentError):
    """Raised (or returned as a signal) when an Arcade tool call needs consent."""
    def __init__(self, url: str, *, tool_name: str | None = None,
                 auth_id: str | None = None) -> None:
        super().__init__(f"arcade authorization required: {url}")
        self.url = url
        self.tool_name = tool_name
        self.auth_id = auth_id


class ArcadeToolError(ArcadeAgentError):
    """Any non-auth Arcade execute failure (validation, upstream, timeout, ...)."""
    def __init__(self, message: str, *, tool_name: str | None = None,
                 kind: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.tool_name = tool_name
        self.kind = kind


class DaemonNotRunning(ArcadeAgentError): ...
class DaemonAlreadyRunning(ArcadeAgentError):
    def __init__(self, pid: int) -> None:
        super().__init__(f"daemon already running (pid={pid})")
        self.pid = pid


class ProtocolError(ArcadeAgentError):
    """Malformed inbound/outbound JSON on the daemon socket."""
```

**Edge cases / contracts.**

- All exceptions subclass `ArcadeAgentError` so the CLI can have a single catch-all that
  formats them with Rich and exits with code 1.
- Carry structured fields (`name`, `url`, `kind`, `pid`) on the exception so callers can
  format them without parsing the message.
- No `from None` re-raises that swallow the original; preserve `__cause__` where helpful.

**Testability.** Pure module, no IO. Tests assert exception type + attributes only.

---

### 5.2 `bindings.py`

Implements the binding registry. **Pure file IO + pydantic.**

**Dependencies:** `tomllib` (stdlib, read), `tomli_w` (write), `pydantic`, `config`,
`errors`.

**File schema** at `config.BINDINGS_FILE`:

```toml
default = "personal"

[[bindings]]
name = "personal"
user_id = "tk@gmail.com"

[[bindings]]
name = "work"
user_id = "tk@work.com"
```

`default` is optional; absent means "no default selected".

**Public API:**

```python
from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel, Field


class Binding(BaseModel):
    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_\-]+$")
    user_id: str = Field(min_length=1)


class BindingStore:
    def __init__(self, path: Path | None = None) -> None: ...
    # path defaults to config.BINDINGS_FILE; allow override for tests.

    def load(self) -> None: ...
    # Read TOML from disk. If file is missing, treat as empty (no error).
    # Raises ConfigError on malformed TOML or schema mismatch.

    def save(self) -> None: ...
    # Atomically write TOML to disk (write to tmp + os.replace).
    # Calls config.ensure_config_dir() first.

    def list(self) -> list[Binding]: ...

    def get(self, name: str) -> Binding: ...
    # Raises BindingNotFound.

    def get_default(self) -> Binding: ...
    # Returns the configured default binding.
    # Raises BindingNotFound if no default is set or the named default is missing.

    def add(self, binding: Binding, *, make_default: bool = False) -> None: ...
    # Raises BindingExists if a binding with the same name is already present.
    # If make_default=True, also updates the default pointer.
    # Calls save() on success.

    def remove(self, name: str) -> None: ...
    # Raises BindingNotFound. If removed binding was the default, clears default.
    # Calls save() on success.

    def set_default(self, name: str) -> None: ...
    # Raises BindingNotFound. Calls save() on success.
```

**Behavior contract.**

- `load()` is called implicitly by every public mutator/reader on the first access of
  an instance (lazy load). A `load()` call always re-reads the file (idempotent).
- All mutators are write-through (call `save()` before returning).
- `save()` uses an atomic write: write to `f"{path}.tmp.{os.getpid()}"`, fsync, then
  `os.replace`. File permissions: chmod 600 on the destination.
- The Pydantic `Binding` validator enforces a friendly name pattern. Duplicate names
  within `bindings` array → `ConfigError` on load.

**Testability hooks.**

- Tests construct `BindingStore(path=tmp_path / "bindings.toml")`. No env var hacks
  required.
- A `binding_store` fixture (see §7.3) yields a pre-loaded store.

**Edge cases.**

| Case | Behavior |
|---|---|
| File missing | Treat as empty, `list()` returns `[]`. |
| File malformed TOML | `ConfigError`. |
| File has unknown top-level keys | Ignore (forward-compat), don't error. |
| Two bindings share a name in the file | `ConfigError` on load. |
| `default` points at a missing binding | `get_default` raises `BindingNotFound`. Other operations OK. |
| Concurrent writers (two processes) | Out of scope; document the limitation. Atomic write ensures the file is never half-written. |
| `add` with a name that violates the regex | Pydantic raises `ValidationError`; let it bubble. The CLI catches and reformats. |

---

### 5.3 `arcade_client.py`

Wraps `arcadepy.AsyncArcade` to expose just what the agent needs.

**Dependencies:** `arcadepy`, `asyncio`, `dataclasses`, `re`, `config`, `errors`.

**Public dataclasses:**

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class AuthResult:
    completed: bool
    url: str | None
    auth_id: str | None


@dataclass(slots=True, frozen=True)
class ExecuteResult:
    success: bool
    output: Any                 # value on success; None otherwise
    auth_url: str | None        # populated when success=False AND auth is the cause
    error_message: str | None   # populated when success=False AND it's a tool error
    error_kind: str | None      # OutputError.kind, when applicable


@dataclass(slots=True, frozen=True)
class AuthURLPending:
    provider_id: str
    auth_id: str | None
    url: str
```

**Class:**

```python
class ArcadeAgentClient:
    def __init__(
        self,
        client: "arcadepy.AsyncArcade",
        tool_names: list[str],
    ) -> None: ...

    async def list_tool_schemas(self) -> list[dict]: ...
    # Returns a list of Anthropic ToolParam dicts:
    #   [{"name": "Gmail_SendEmail", "description": "...", "input_schema": {...}}, ...]
    # Implementation:
    #   1. Group self.tool_names by toolkit prefix (the substring before the first '.').
    #   2. For each toolkit, paginate client.tools.list(toolkit=<TK>,
    #      include_format=["anthropic"], limit=100) and collect ToolDefinitions whose
    #      qualified_name is in self.tool_names.
    #   3. Convert each ToolDefinition into an Anthropic ToolParam:
    #         arcade_name   = td.qualified_name
    #         anthropic_name = _normalize_name(arcade_name)
    #         description   = td.description or ""
    #         input_schema  = _build_input_schema(td.input) if td.formatted_schema is None
    #                         else td.formatted_schema["input_schema"]
    #      Build a JSON-Schema object: {"type":"object","properties":{...},"required":[...]}
    #      from td.input.parameters when formatted_schema is unavailable.
    #   4. Cache the (arcade <-> anthropic) name map in self._arcade_to_anthropic /
    #      self._anthropic_to_arcade. Raise ConfigError if a duplicate Anthropic name
    #      would collide.
    # Idempotent: caches the result on the instance after the first call.
    # Raises ConfigError if a requested tool from self.tool_names is not returned by
    # Arcade (so a typo in config.py fails fast at startup).

    def arcade_name_for(self, anthropic_name: str) -> str: ...
    # Reverse lookup; raises KeyError if list_tool_schemas() has not been called yet.

    async def authorize(self, user_id: str, tool_name: str) -> AuthResult: ...
    # Calls client.tools.authorize(tool_name=tool_name, user_id=user_id).
    # Maps the AuthorizationResponse:
    #   status == "completed"  -> AuthResult(completed=True, url=None, auth_id=resp.id)
    #   else                   -> AuthResult(completed=False, url=resp.url, auth_id=resp.id)
    # Raises ArcadeToolError on transport failure (HTTP, parsing).

    async def wait_for_completion(self, auth_id: str) -> None: ...
    # Calls client.auth.wait_for_completion(auth_id).
    # Wrapped in asyncio.wait_for(..., timeout=300).
    # Raises ArcadeToolError on timeout/failure status.

    async def execute(
        self,
        user_id: str,
        tool_name: str,
        input_dict: dict[str, Any],
    ) -> ExecuteResult: ...
    # tool_name MUST be the Arcade (dotted) form, e.g. "Gmail.SendEmail".
    # Calls client.tools.execute(tool_name=tool_name, user_id=user_id, input=input_dict),
    # wrapped in asyncio.wait_for(..., timeout=120).
    # Mapping rules:
    #   - response.success is True:
    #         ExecuteResult(success=True, output=response.output.value,
    #                       auth_url=None, error_message=None, error_kind=None)
    #   - response.success is False AND
    #     (response.output.authorization is not None
    #      or response.output.error.kind == "TOOL_REQUIREMENTS_NOT_MET"):
    #         url = response.output.authorization.url if response.output.authorization
    #               else None
    #         If url is None: call self.authorize(user_id, tool_name) to fetch one.
    #         ExecuteResult(success=False, output=None, auth_url=url,
    #                       error_message=None, error_kind="AUTH_REQUIRED")
    #   - response.success is False otherwise:
    #         ExecuteResult(success=False, output=None, auth_url=None,
    #                       error_message=response.output.error.message,
    #                       error_kind=response.output.error.kind)
    # Does NOT raise on tool/auth failure; only raises ArcadeToolError on transport,
    # timeout, or malformed-response failures.

    async def pre_authorize_all(self, user_id: str) -> list[AuthURLPending]: ...
    # For each unique toolkit in self.tool_names (e.g. {"Gmail", "GoogleCalendar"}),
    # picks ONE representative tool and calls self.authorize(user_id, rep_tool).
    # Returns AuthURLPending entries for the ones that came back not-completed.
    # Strategy: pick the first tool in self.tool_names per toolkit prefix.
    # Used by `cli bind add` to drive consent for all relevant providers in one shot.
```

**Name normalization** (module-private helper):

```python
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

def _normalize_name(arcade_name: str) -> str:
    """Gmail.SendEmail -> Gmail_SendEmail."""
    n = arcade_name.replace(".", "_")
    if not _NAME_RE.match(n):
        raise ConfigError(f"tool name not Anthropic-compatible after normalization: {n!r}")
    return n
```

**Behavior contract.**

- `list_tool_schemas()` may be called many times; only the first call hits Arcade.
- A successful `authorize` whose response already has status `"completed"` returns
  `AuthResult(completed=True, url=None, auth_id=...)`. The caller MUST treat
  `completed=True` as "no consent needed; do not open a browser".
- `execute` NEVER raises on tool-side errors (auth or runtime). It returns
  `ExecuteResult` with `success=False`. Transport-layer errors (HTTP 5xx, JSON parse,
  timeout) raise `ArcadeToolError`.
- The reverse name map is the only mutable instance state besides the cached schemas.
  No globals.

**Testability hooks.**

- Constructor takes the `AsyncArcade` instance, so tests pass a `MagicMock(spec=
  AsyncArcade)` and stub `mock.tools.list`, `mock.tools.authorize`, `mock.tools.execute`,
  `mock.auth.wait_for_completion`.
- `tools.list` returns an async paginator; tests should provide an async iterator
  whose items are `ToolDefinition` instances (or plain dataclasses that quack like
  one — the conversion code MUST access via attribute, not `getattr` with default,
  so missing fields surface in tests).
- A `mock_arcade` fixture (see §7.3) wires all four endpoints with default happy-path
  return values; tests override per-case.

**Edge cases.**

| Case | Behavior |
|---|---|
| `tool_names` references a tool Arcade does not return | `ConfigError` on `list_tool_schemas`. |
| `tools.list` returns 0 results for a toolkit | `ConfigError`. |
| `formatted_schema` missing in a ToolDefinition | Fall back to building JSON-Schema from `td.input.parameters` (use `param.value_schema.val_type` → JSON-Schema type). Required = `param.required is True`. |
| `_normalize_name` collision (two tools normalize to the same Anthropic name) | `ConfigError`. Unlikely with the configured tool list but enforce it. |
| `execute` returns `response.output is None` | Treat as `ArcadeToolError("empty Arcade response")`. |
| `authorize` returns `status="failed"` | `AuthResult(completed=False, url=resp.url, auth_id=resp.id)`. Caller decides; daemon will surface as `error` event. |
| `wait_for_completion` times out (asyncio) | `ArcadeToolError("auth wait timed out")`. |
| Network error from any call | `ArcadeToolError(str(exc))`, chained via `from exc`. |
| `pre_authorize_all` called with no `tool_names` | Returns `[]`. |
| Binding deleted mid-call | `arcade_client` does not know about bindings; daemon handles that race (§5.6). |

---

### 5.4 `confirm.py`

Pure helpers for the confirmation gate. **No IO. No async.**

**Dependencies:** `config`. That's it.

**Public API:**

```python
from __future__ import annotations
from typing import Any


def is_destructive(tool_name: str) -> bool:
    """True iff the *Arcade* tool_name (dotted form) is in config.DESTRUCTIVE_TOOLS."""


def render_summary(tool_name: str, args: dict[str, Any]) -> str:
    """Multi-line human-readable summary for the confirmation prompt.

    Layout:
        Tool: <tool_name>
        Arguments:
          <key>: <repr-truncated value>
          ...

    Truncation rules (applied per value):
      - str values longer than 500 chars: keep first 500 + " […+<N> more chars]"
      - bytes/bytearray: shown as "<N bytes>"
      - lists/dicts: json.dumps with indent=2, truncated to 500 chars total
      - everything else: repr(), truncated to 500 chars total
    Keys are sorted alphabetically for deterministic output.
    """
```

**Behavior contract.**

- Pure. Deterministic. Same input → same output, byte-for-byte.
- Never raises (catch any `TypeError` from `json.dumps` and fall back to `repr`).

**Testability.** Snapshot-style tests with hand-rolled fixtures: long bodies, weird
types, empty args, etc.

**Edge cases.**

- Empty `args` → produces `Tool: X\nArguments: (none)\n`.
- Nested dicts → use `json.dumps(value, default=str, indent=2)` then truncate.
- Non-JSON-serializable value → fall back to `repr`.

---

### 5.5 `claude_agent.py`

The tool-use loop. **The only module that talks to Anthropic.**

**Dependencies:** `anthropic`, `asyncio`, `logging`, `dataclasses`, `typing`,
`arcade_client`, `confirm`, `config`, `errors`.

**Public dataclasses** (sealed event union):

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Union


@dataclass(slots=True, frozen=True)
class TextDelta:
    text: str

@dataclass(slots=True, frozen=True)
class ToolCallStarted:
    name: str                  # Arcade dotted name
    args: dict[str, Any]

@dataclass(slots=True, frozen=True)
class ToolCallResult:
    name: str
    ok: bool
    summary: str               # short human-readable result

@dataclass(slots=True, frozen=True)
class AuthURLRequested:
    url: str

@dataclass(slots=True, frozen=True)
class ConfirmRequested:
    tool_name: str
    args: dict[str, Any]

@dataclass(slots=True, frozen=True)
class Final:
    text: str

@dataclass(slots=True, frozen=True)
class ErrorEvent:
    message: str


Event = Union[
    TextDelta, ToolCallStarted, ToolCallResult,
    AuthURLRequested, ConfirmRequested, Final, ErrorEvent,
]
```

**Class:**

```python
from collections.abc import AsyncIterator, Awaitable, Callable

OnAuthURL = Callable[[str], Awaitable[None]]
OnConfirm = Callable[[str, dict[str, Any]], Awaitable[bool]]


class ClaudeAgent:
    def __init__(
        self,
        anthropic_client: "anthropic.AsyncAnthropic",
        arcade_client: ArcadeAgentClient,
        model: str,
        on_auth_url: OnAuthURL,
        on_confirm: OnConfirm,
        *,
        max_tokens: int = 4096,
        max_iterations: int = 12,
        system_prompt: str | None = None,
    ) -> None: ...

    async def run_turn(
        self,
        user_id: str,
        prompt: str,
        history: list[dict],
    ) -> AsyncIterator[Event]: ...
```

**`run_turn` algorithm (normative):**

1. Lazy-init: `tools = await self._arcade.list_tool_schemas()` on first call (cache on
   the instance — they are user-independent).
2. Append `{"role": "user", "content": prompt}` to `history`.
3. Loop up to `max_iterations` (default 12):
   1. Call `self._anthropic.messages.create(model=self._model,
      max_tokens=self._max_tokens, system=self._system_prompt or NOT_GIVEN,
      tools=tools, messages=history)`.
   2. Append the assistant message to `history` as
      `{"role": "assistant", "content": [block.model_dump() for block in resp.content]}`.
   3. For each block in `resp.content`:
      - `TextBlock` → `yield TextDelta(text=block.text)`.
      - `ToolUseBlock` → handle per "Tool call handling" below; collect a
        `tool_result` content item per call.
      - other block types → ignore.
   4. If `resp.stop_reason == "tool_use"`:
      - Append `{"role": "user", "content": <list of tool_result items>}` to history.
      - `continue` the loop.
   5. Else (`end_turn`, `max_tokens`, `stop_sequence`, `pause_turn`, `refusal`):
      - Concatenate text blocks from `resp.content` into `final_text`.
      - If `stop_reason == "refusal"`:
            `yield ErrorEvent("model refused to answer")`.
            `yield Final(text=final_text or "")`. return.
      - Else `yield Final(text=final_text)`. return.
4. If the loop falls off the end (hit `max_iterations`):
   `yield ErrorEvent("hit max tool-use iterations"); yield Final(text="")`.

**Tool call handling (per `ToolUseBlock`):**

1. `arcade_name = self._arcade.arcade_name_for(block.name)`
   (KeyError → `ErrorEvent("unknown tool name from model: ...")` and craft a
   `is_error=True` tool_result with that message; continue with next block).
2. `yield ToolCallStarted(name=arcade_name, args=block.input)`.
3. If `is_destructive(arcade_name)`:
   - `yield ConfirmRequested(tool_name=arcade_name, args=block.input)`.
   - `approved = await self._on_confirm(arcade_name, block.input)`.
   - If not approved: tool_result content = `f"User declined to run {arcade_name}."`,
     `is_error=True`, and `yield ToolCallResult(name=arcade_name, ok=False,
     summary="user declined")`. Continue.
4. Call `result = await self._arcade.execute(user_id, arcade_name, block.input)`.
5. Branch on `result`:
   - `success=True`:
     - Stringify `result.output` to `str` (use `json.dumps(default=str)` if not
       already a string; truncate at 8 KB and append " […truncated]" if longer).
     - tool_result content = that string, `is_error=False`.
     - `yield ToolCallResult(name=arcade_name, ok=True,
       summary=<first 200 chars of stringified output>)`.
   - `success=False` AND `auth_url` is not None:
     - `await self._on_auth_url(result.auth_url)`;
       `yield AuthURLRequested(url=result.auth_url)`.
     - `await self._arcade.wait_for_completion(auth_id=<we don't have one here;
       call self._arcade.authorize(user_id, arcade_name) to get the auth_id then
       wait>)`. Implementation note: simpler — call `authorize` to get a fresh
       `AuthResult`, if `completed=False` then `wait_for_completion(result.auth_id)`.
     - Re-run `self._arcade.execute(user_id, arcade_name, block.input)` exactly once.
     - On the retry: same success/failure mapping as above.
     - If retry still requires auth → tool_result is_error with
       message "authorization not completed".
   - `success=False` (plain tool error):
     - tool_result content = `result.error_message or "tool failed"`, `is_error=True`.
     - `yield ToolCallResult(name=arcade_name, ok=False, summary=result.error_message
       or result.error_kind or "tool failed")`.
6. Wrap the `arcade.execute` call in a `try/except ArcadeToolError as exc:` —
   on transport failures, emit `ErrorEvent(exc.message)` and craft a tool_result with
   `is_error=True, content=str(exc)`; do NOT raise out of `run_turn`.

**Behavior contract.**

- `run_turn` is an **async generator**. Callers must iterate to completion (or
  `aclose()` it) for cleanup to fire.
- `history` is mutated in place. On the next call to `run_turn(... history=history)`
  the same list keeps the conversation. The caller (daemon) owns the per-session list.
- Pricing/usage tracking is out of scope; log `resp.usage.input_tokens` /
  `output_tokens` at DEBUG level.
- The agent NEVER prints to stdout. All user-facing output flows through `Event` yields.

**Testability hooks.**

- Constructor takes both clients, so tests pass `MagicMock(spec=AsyncAnthropic)` and
  the (already mockable) `ArcadeAgentClient`.
- `on_auth_url` and `on_confirm` are easy to fake (`AsyncMock`).
- A `mock_anthropic` fixture (§7.3) provides helper builders for `Message` responses
  with `text`/`tool_use` blocks and configurable `stop_reason`.

**Edge cases.**

| Case | Behavior |
|---|---|
| Model emits no content blocks | Treat as `stop_reason="end_turn"` with empty text; `yield Final("")`. |
| Model emits a `ToolUseBlock` for an unknown name | Craft `is_error=True` tool_result + `ErrorEvent`; loop continues so the model can recover. |
| `on_confirm` raises | Propagate as `ErrorEvent` and abort the turn with `Final("")`. |
| `on_auth_url` raises | Same. |
| `ArcadeToolError` from `execute` | `ErrorEvent` + `is_error` tool_result; loop continues. |
| `ArcadeToolError` from `authorize`/`wait_for_completion` | `ErrorEvent` + `is_error` tool_result with the message; loop continues. |
| `max_iterations` reached | `ErrorEvent("hit max tool-use iterations")` + `Final("")`. |
| Refusal (`stop_reason="refusal"`) | `ErrorEvent("model refused to answer")` + `Final(<text>)`. |
| `stop_reason="pause_turn"` | Treat like end_turn for v1; emit `Final` and stop. |

---

### 5.6 `daemon.py`

UDS server + lifecycle (start/stop, pidfile, double-fork detach).

**Dependencies:** `asyncio`, `os`, `sys`, `stat`, `json`, `logging`, `signal`,
`pathlib`, `uuid`, `dataclasses`, `claude_agent`, `arcade_client`, `bindings`,
`config`, `errors`.

**Public surface:**

```python
from __future__ import annotations
from collections.abc import Callable, Awaitable

# Factory type used so the daemon can build a ClaudeAgent per-connection without
# baking in module-level singletons.
AgentFactory = Callable[[], "ClaudeAgent"]


class DaemonServer:
    def __init__(
        self,
        agent_factory: AgentFactory,
        binding_store: "BindingStore",
        *,
        socket_path: "Path" = config.SOCKET_PATH,
        pidfile: "Path" = config.PIDFILE,
    ) -> None: ...

    async def start(self) -> None: ...
    # 1. config.ensure_config_dir()
    # 2. Check pidfile: if present and pid is alive, raise DaemonAlreadyRunning(pid).
    #    Otherwise remove stale pidfile.
    # 3. Remove any stale socket at socket_path (if not bound).
    # 4. Write os.getpid() to pidfile.
    # 5. Start asyncio.start_unix_server(self._handle, path=str(socket_path)).
    # 6. os.chmod(socket_path, 0o600).
    # 7. Install SIGTERM/SIGINT handlers that call self.stop() and exit cleanly.
    # 8. await server.serve_forever().

    async def stop(self) -> None: ...
    # 1. Close all open connections.
    # 2. Close the server.
    # 3. Unlink socket_path (ignore FileNotFoundError).
    # 4. Unlink pidfile (ignore FileNotFoundError).
    # Idempotent.

    # --- internals (documented because the integration test stubs them) ---
    async def _handle(self,
                      reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None: ...
    # See "Connection handling" below.
```

**Module-level helpers:**

```python
def start_detached(log_path: "Path" = config.CONFIG_DIR / "daemon.log") -> int:
    """Double-fork the current process. Returns the daemon PID to the parent.

    Implementation (POSIX only):
      - First fork; parent waits for child and returns the grandchild's pid.
      - In child: os.setsid(), umask(0o077), chdir("/"), fork again.
      - Grandchild: redirect stdin <- /dev/null, stdout/stderr -> log_path (append).
      - Grandchild calls _daemon_main() which constructs deps from env and runs
        asyncio.run(DaemonServer(...).start()).
      - Intermediate child exits immediately.
    On non-POSIX (Windows): raise RuntimeError("detach not supported").
    """


def is_running(pidfile: "Path" = config.PIDFILE) -> int | None:
    """Return pid if pidfile points at a live process; else None.
    On stale pidfile, the function does NOT delete it (start() handles cleanup)."""
```

**Connection handling (`_handle`):**

Each connection is a single conversation. Inbound and outbound are NDJSON.

1. Generate `session_id = str(uuid.uuid4())` if the first inbound message does
   not provide one; otherwise use the provided one.
2. Loop forever (until EOF or write error):
   1. `line = await reader.readline()`. If empty → break.
   2. Try `msg = json.loads(line)`; on `JSONDecodeError`:
      `await _send(writer, {"event": "error", "message": "malformed JSON"})`; continue.
   3. Validate top-level shape (`op` is a string). On failure:
      send `{"event":"error","message":"missing 'op'"}`; continue.
   4. Dispatch on `msg["op"]`:
      - `"prompt"`: see below.
      - `"confirm_ack"`: deliver `msg["approved"]` to the pending `asyncio.Future`
        for `msg["session_id"]`. If no pending confirm → send `error`.
      - unknown op → send `error`.
3. On disconnect: cancel any in-flight task for this connection; drop the
   per-session history? **NO** — keep it in the in-memory store keyed by `session_id`
   so a reconnecting client can resume by reusing the id.

**Prompt handling:**

1. Look up `binding = self._bindings.get(msg["binding"])`. On `BindingNotFound`:
   send `{"event":"error","message":"unknown binding 'X'"}`; continue the loop.
2. Get or create `history = self._history.setdefault(session_id, [])` and
   `lock = self._locks.setdefault(session_id, asyncio.Lock())`.
3. Build `on_auth_url`: `async def f(url): await _send(writer, {"event":"auth_url","url":url})`.
4. Build `on_confirm`:
   - Create `fut = asyncio.get_running_loop().create_future()`.
   - Register it as `self._pending_confirms[session_id] = fut`.
   - `await _send(writer, {"event":"confirm","tool":tool_name,"args":args,
     "session_id":session_id})`.
   - `approved = await asyncio.wait_for(fut, timeout=300)`.
   - `del self._pending_confirms[session_id]`.
   - Return `approved`.
5. Build the agent: `agent = self._make_agent_with_callbacks(on_auth_url, on_confirm)`.
   (`agent_factory()` returns a stub; the daemon wraps it with the per-request
   callbacks. The clean way is to make `agent_factory` itself take both callbacks:
   `AgentFactory = Callable[[OnAuthURL, OnConfirm], ClaudeAgent]`. Use that form.)
6. Under `async with lock:`:
   ```
   async for event in agent.run_turn(binding.user_id, msg["prompt"], history):
       await _send_event(writer, event)
   ```
7. After the generator finishes, the next inbound `prompt` on this connection works
   the same way (same `session_id` → same history → multi-turn chat).

**Outbound mapping** (single source of truth — keep in sync with §6):

| Event dataclass | JSON line |
|---|---|
| `TextDelta(text)` | `{"event":"text","delta":text}` |
| `ToolCallStarted(name,args)` | `{"event":"tool_call","name":name,"args":args}` |
| `ToolCallResult(name,ok,summary)` | `{"event":"tool_result","name":name,"ok":ok,"summary":summary}` |
| `AuthURLRequested(url)` | `{"event":"auth_url","url":url}` |
| `ConfirmRequested(tool,args)` | `{"event":"confirm","tool":tool,"args":args,"session_id":session_id}` |
| `Final(text)` | `{"event":"final","text":text}` |
| `ErrorEvent(message)` | `{"event":"error","message":message}` |

**Behavior contract.**

- `start()` raises `DaemonAlreadyRunning(pid)` if pidfile points at a live process.
  Stale pidfile (file present, process gone) is silently cleared.
- `start()` is awaitable but never returns under normal operation (runs forever).
  Use `start_detached()` from the CLI to background it.
- `stop()` is safe to call from a signal handler; uses `loop.call_soon_threadsafe`
  if needed.
- A connection error during write must not crash the daemon — log a warning and drop
  that connection.
- The per-session lock prevents two concurrent `prompt` ops mutating the same
  history. Multiple connections with **different** `session_id`s run in parallel.

**Testability hooks.**

- Constructor injects `agent_factory` and `binding_store`. Tests pass a fake
  `agent_factory` that returns a `ClaudeAgent`-shaped object whose `run_turn` yields
  scripted events.
- `socket_path` and `pidfile` overridable, so tests use `tmp_path`.
- The integration test (§7.2) uses a real Unix socket but a fake agent.

**Edge cases.**

| Case | Behavior |
|---|---|
| Socket file exists from a previous crash, no live daemon | Unlink before bind. |
| Pidfile present, process alive | `DaemonAlreadyRunning(pid)`. |
| Pidfile present, process dead | Delete pidfile, continue. |
| Pidfile present, can't parse | Treat as stale; delete and continue. |
| Inbound JSON malformed | Send `error`; keep the connection open. |
| Inbound JSON missing required field | Send `error`; keep connection open. |
| `confirm_ack` arrives for unknown session_id | Send `error`. |
| Client disconnects mid-prompt | Cancel the task running `run_turn`; keep `history` in `self._history`. Do not raise. |
| `on_confirm` future never resolved (timeout 300 s) | Treat as `approved=False`; emit `ErrorEvent("confirmation timed out")`. |
| `BindingNotFound` mid-turn (binding was removed) | The lookup happens once at prompt start; if the binding is removed afterwards, the in-flight call still uses the resolved `user_id`. The next `prompt` will see `BindingNotFound` and emit `error`. |
| `SIGTERM` mid-turn | Cancel all session tasks, drain writers with a 5 s budget, unlink socket+pidfile, exit. |
| OS does not support Unix sockets (Windows) | `start()` raises `ConfigError("Unix sockets are required")`. |

---

### 5.7 `cli.py` (replaces current stub)

Typer app with subcommand groups. Replaces the existing minimal `cli.py`.

**Dependencies:** `typer`, `rich`, `webbrowser`, `dotenv`, `asyncio`, `json`,
`os`, `socket`, `sys`, `pathlib`, `arcade_agent.{config,bindings,arcade_client,
claude_agent,daemon,errors}`, plus `arcadepy` and `anthropic` at the edges
(only inside command functions, not module-level imports — keep `arcade-agent --help`
snappy).

**App layout:**

```python
app          = typer.Typer(name="arcade-agent", no_args_is_help=True,
                           help="Gmail + Calendar agent powered by Arcade.dev "
                                "and Anthropic Claude.")
bind_app     = typer.Typer(name="bind", no_args_is_help=True,
                           help="Manage Arcade user bindings.")
daemon_app   = typer.Typer(name="daemon", no_args_is_help=True,
                           help="Control the background daemon.")
app.add_typer(bind_app, name="bind")
app.add_typer(daemon_app, name="daemon")
```

**Top-level callback:** `app.callback()` loads `.env` (via `dotenv.load_dotenv()`)
and instantiates a single `rich.Console` stored on a `typer.Context.obj` namespace.

**Commands:**

```python
@app.command("version")
def version() -> None: ...
# Prints arcade_agent.__version__.


@app.command("init")
def init(force_env: bool = typer.Option(False, "--force-env",
         help="Recreate .env even if it exists.")) -> None: ...
# Interactive flow:
#   1. Check ARCADE_API_KEY and ANTHROPIC_API_KEY in env.
#      For each missing key: print a Rich warning and offer to write a .env file
#      (cwd-local) with placeholders or with values entered interactively
#      (typer.prompt(..., hide_input=True)).
#   2. Print a header explaining the bind flow.
#   3. Prompt for a binding name (default "personal") and a user_id (email).
#   4. Call bind add NAME --user-id USER (same code path).


@bind_app.command("add")
def bind_add(
    name: str = typer.Argument(...),
    user_id: str = typer.Option(..., "--user-id", "-u"),
    make_default: bool = typer.Option(False, "--default"),
) -> None: ...
# Flow:
#   1. Build BindingStore() and add the binding (raises BindingExists -> exit 1).
#   2. Construct AsyncArcade(api_key=os.environ["ARCADE_API_KEY"])  (env required).
#   3. Construct ArcadeAgentClient(client, config.ALL_TOOLS).
#   4. pending = await client.pre_authorize_all(user_id).
#      For each AuthURLPending:
#        - console.print rich panel with the URL.
#        - try webbrowser.open(url); ignore failures.
#        - await client.wait_for_completion(p.auth_id) with a spinner.
#   5. Print success.


@bind_app.command("list")
def bind_list() -> None: ...     # rich.table.Table, mark default with *.


@bind_app.command("remove")
def bind_remove(name: str) -> None: ...     # confirmation prompt, then remove.


@bind_app.command("reauth")
def bind_reauth(name: str) -> None: ...     # same flow as bind_add step 2-5 only.


@bind_app.command("set-default")
def bind_set_default(name: str) -> None: ...


@daemon_app.command("start")
def daemon_start(
    foreground: bool = typer.Option(False, "--foreground", "-f"),
) -> None: ...
# - Check env vars; bail with ConfigError if missing.
# - If is_running(): print "already running (pid=N)" and exit 0.
# - If --foreground: build deps and run DaemonServer.start() in the current process.
# - Else: call daemon.start_detached() and print "started (pid=N), logs at ...".


@daemon_app.command("stop")
def daemon_stop() -> None: ...
# - Read pidfile. If absent: print "not running" and exit 0.
# - Send SIGTERM. Poll up to 5 s for the process to exit; SIGKILL on timeout.
# - Print "stopped".


@daemon_app.command("status")
def daemon_status() -> None: ...
# - Print pid (or "not running"), socket path (and whether it exists), log path.


@app.command("ask")
def ask(
    prompt: str = typer.Argument(...),
    binding: str | None = typer.Option(None, "--binding", "-b"),
) -> None: ...
# - Resolve binding (None -> BindingStore.get_default()).
# - asyncio.run(_send_and_stream(binding_name, prompt, session_id=uuid4())).


@app.command("chat")
def chat(
    binding: str | None = typer.Option(None, "--binding", "-b"),
) -> None: ...
# - REPL using a stable session_id for the whole session.
# - prompt_toolkit not required; use typer.prompt or input(). Multi-line not needed.
# - "/exit" or EOF quits cleanly.
```

**Streaming helper:**

```python
async def _send_and_stream(
    binding_name: str,
    prompt: str,
    *,
    session_id: str,
    console: "rich.console.Console",
) -> None:
    """Open the UDS, send a prompt, render events until 'final' or 'error'."""
```

Algorithm:

1. `reader, writer = await asyncio.open_unix_connection(str(config.SOCKET_PATH))`
   On `FileNotFoundError` / `ConnectionRefusedError`:
   raise `DaemonNotRunning()` → caught at command level, prints
   "Daemon not running. Run `arcade-agent daemon start` first." and exits 1.
2. Send `{"op":"prompt","binding":binding_name,"prompt":prompt,"session_id":session_id}`
   as one NDJSON line.
3. Loop reading lines:
   - `text` → `console.print(delta, end="")` (no newline).
   - `tool_call` → `console.print(Panel(...))` with a dim color.
   - `tool_result` → `console.print` a one-line summary (green for ok, red for not).
   - `auth_url` → `webbrowser.open(url)`; also print the URL as fallback.
   - `confirm` → `Confirm.ask(render_summary(tool,args), default=False)`;
     send `{"op":"confirm_ack","session_id":sid,"approved":bool}`.
   - `final` → print and break.
   - `error` → print red and break.
4. Close the writer; await `wait_closed()`.

**Rendering:**

- Use `rich.panel.Panel` for tool calls and confirmations.
- Use `rich.live.Live` for the streaming text? NOT required v1 — newline-less
  `console.print` is fine.
- Final assistant text gets its own panel with title `"assistant"`.

**Behavior contract.**

- Every command catches `ArcadeAgentError` at the boundary and exits with code 1
  after printing the message via Rich.
- `init`, `bind add`, `bind reauth` are the only commands that touch network from
  the CLI itself; everything else goes through the daemon.

**Testability hooks.**

- Tests use `typer.testing.CliRunner` and `monkeypatch` to swap `BindingStore`,
  `AsyncArcade`, `ArcadeAgentClient`, and `asyncio.open_unix_connection`.
- The `_send_and_stream` helper takes a `console` parameter so tests can capture
  output.

**Edge cases.**

| Case | Behavior |
|---|---|
| Missing `ARCADE_API_KEY` | `ConfigError("ARCADE_API_KEY missing")`; exit 1 with red message and pointer to `arcade-agent init`. |
| Missing `ANTHROPIC_API_KEY` | Same. |
| `arcade-agent ask` with no daemon running | `DaemonNotRunning`; exit 1 with a hint. |
| `arcade-agent ask` with daemon socket existing but not accepting (stale) | Same as DaemonNotRunning. Detect via `ConnectionRefusedError`. |
| `arcade-agent daemon start` twice | Second call sees `is_running()` and exits 0 with a notice. |
| `arcade-agent daemon stop` without pidfile | Exit 0 with "not running". |
| User answers "n" at a confirm prompt | Send `approved: false`; the agent continues; CLI keeps reading events. |
| `/exit` in `chat` | Close writer, exit 0. |
| Ctrl-C during `chat` | Cancel current request, close writer, exit 0. |
| Ctrl-C during `daemon start --foreground` | Trigger `DaemonServer.stop()`; exit 0. |
| Default binding requested but none set | `BindingNotFound`; print "No default binding. Use --binding or run `arcade-agent bind set-default NAME`."; exit 1. |

---

## 6. Wire protocol — daemon UDS

Single document. **`cli.py` and `daemon.py` MUST match this byte-for-byte.**

### Transport

- AF_UNIX, SOCK_STREAM at `config.SOCKET_PATH`, file mode `0o600`.
- Each message is one JSON object followed by `\n`. No length prefix.
- Encoding: UTF-8.
- Connection is bidirectional and long-lived; multiple prompts may flow over one
  connection (used by `chat`).

### Inbound (CLI → daemon)

```jsonc
// New prompt
{"op": "prompt", "binding": "personal", "prompt": "...", "session_id": "<uuid>"}

// User answered a confirmation
{"op": "confirm_ack", "session_id": "<uuid>", "approved": true}
```

`session_id` is REQUIRED on every inbound message. The CLI generates it (uuid4) and
reuses it across multiple `prompt` ops in `chat`.

### Outbound (daemon → CLI)

```jsonc
{"event": "text",        "delta":   "string fragment"}
{"event": "tool_call",   "name":    "Gmail.SendEmail", "args": {...}}
{"event": "tool_result", "name":    "Gmail.SendEmail", "ok": true, "summary": "..."}
{"event": "auth_url",    "url":     "https://..."}
{"event": "confirm",     "tool":    "Gmail.SendEmail", "args": {...}, "session_id": "<uuid>"}
{"event": "final",       "text":    "final assistant message"}
{"event": "error",       "message": "..."}
```

Stream invariant: a successful turn ends with **exactly one** `final` or one `error`
event. After that the connection stays open for the next `prompt`.

---

## 7. Testing strategy

Pytest + `pytest-asyncio` (asyncio_mode = "auto" is set in `pyproject.toml`). All
external IO is mocked except in §7.2 (real socket) and §7.4 (live probe).

### 7.1 Per-module unit tests

| Test file | Covers | External deps |
|---|---|---|
| `test_bindings.py` | `BindingStore` round-trip, regex validation, default semantics, atomic write, malformed file → `ConfigError`. | tmp_path |
| `test_arcade_client.py` | name normalization, `list_tool_schemas` builds Anthropic dicts, `execute` mapping for all 3 branches (success / auth required / tool error), `authorize` and `wait_for_completion` happy & sad paths, `pre_authorize_all` skips completed providers. | `mock_arcade` |
| `test_confirm.py` | `is_destructive` table-driven against `config.DESTRUCTIVE_TOOLS`; `render_summary` snapshot tests for short, long-body, empty, weird-type cases. | none |
| `test_claude_agent.py` | Single text reply (no tools); single tool_use → success → final; auth required → emits `AuthURLRequested` then re-executes; confirm path on destructive tool (approve + deny); refusal stop_reason; max_iterations cap; transport error in execute. | `mock_arcade`, `mock_anthropic` |
| `test_cli.py` | `version`, `bind add` (mocks `AsyncArcade`, `pre_authorize_all`), `bind list/remove/set-default`, `ask` with no daemon (asserts `DaemonNotRunning` exit), `ask` happy path through a mocked `open_unix_connection`. | `CliRunner`, monkeypatches |

Mocking conventions:

- Always `MagicMock(spec=AsyncArcade)` / `MagicMock(spec=AsyncAnthropic)` so typos in
  attribute access fail loudly.
- For async methods on the mock: `mock.tools.execute = AsyncMock(return_value=...)`.
- Build real `ToolDefinition` instances (the pydantic models from `arcadepy.types`)
  rather than dicts when feeding `_list_tool_schemas` to exercise the same attribute
  access the implementation uses.

### 7.2 Daemon integration test

`test_daemon.py` uses a **real Unix socket** but a fake agent:

1. Build `DaemonServer(agent_factory=fake_factory, binding_store=binding_store,
   socket_path=tmp_path/"agent.sock", pidfile=tmp_path/"daemon.pid")`.
2. Start the server in a background task: `task = asyncio.create_task(server.start())`.
   Wait until the socket file exists (poll up to 2 s).
3. As a client: `reader, writer = await asyncio.open_unix_connection(...)`.
4. Send a `{"op":"prompt"...}` line.
5. Read lines until `event in {"final", "error"}`.
6. Assert sequence of events matches `fake_factory`'s scripted yields.

Scripted scenarios:

- Plain text reply (no tools): one `text`, one `final`.
- Tool call with confirm: receive `confirm` event, send `confirm_ack:true`, receive
  `tool_call`, `tool_result`, `final`.
- Tool call needing auth: receive `auth_url`, then `tool_call`, `tool_result`, `final`.
- Malformed inbound: send `not-json\n`; assert `{"event":"error",...}` and that the
  connection stays open (send a valid prompt next; it works).
- Two parallel connections with different `session_id` finish independently.

Teardown: `await server.stop()`, `await task` (it will return after stop).

### 7.3 `tests/conftest.py` outline

```python
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcade_agent import config as _config
from arcade_agent.bindings import Binding, BindingStore


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CONFIG_DIR + derived paths to a tmp dir for the test."""
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(_config, "BINDINGS_FILE", tmp_path / "bindings.toml")
    monkeypatch.setattr(_config, "SOCKET_PATH", tmp_path / "agent.sock")
    monkeypatch.setattr(_config, "PIDFILE", tmp_path / "daemon.pid")
    return tmp_path


@pytest.fixture
def binding_store(tmp_config_dir: Path) -> BindingStore:
    store = BindingStore()
    store.add(Binding(name="personal", user_id="user@example.com"), make_default=True)
    return store


@pytest.fixture
def mock_arcade() -> MagicMock:
    """A MagicMock shaped like arcadepy.AsyncArcade with happy-path defaults."""
    from arcadepy import AsyncArcade
    from arcadepy.types.tool_definition import (
        ToolDefinition, Input, InputParameter, Toolkit, Requirements,
        RequirementsAuthorization,
    )
    from arcadepy.types.value_schema import ValueSchema
    from arcadepy.types.execute_tool_response import ExecuteToolResponse, Output
    from arcadepy.types.shared.authorization_response import AuthorizationResponse

    mock = MagicMock(spec=AsyncArcade)
    # Wire async sub-resources
    mock.tools = MagicMock()
    mock.tools.list = MagicMock(return_value=_AsyncIterPage([...]))  # see helper
    mock.tools.authorize = AsyncMock(return_value=AuthorizationResponse(
        id="auth_1", status="completed", url=None,
    ))
    mock.tools.execute = AsyncMock(return_value=ExecuteToolResponse(
        success=True, output=Output(value={"ok": True}),
    ))
    mock.auth = MagicMock()
    mock.auth.wait_for_completion = AsyncMock(return_value=AuthorizationResponse(
        id="auth_1", status="completed", url=None,
    ))
    return mock


@pytest.fixture
def mock_anthropic() -> MagicMock:
    """MagicMock shaped like anthropic.AsyncAnthropic with helpers attached."""
    import anthropic
    from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

    mock = MagicMock(spec=anthropic.AsyncAnthropic)
    mock.messages = MagicMock()
    mock.messages.create = AsyncMock()

    def make_message(*blocks, stop_reason="end_turn") -> Message:
        return Message(
            id="msg_test",
            role="assistant",
            content=list(blocks),
            model="claude-sonnet-4-5",
            stop_reason=stop_reason,
            stop_sequence=None,
            type="message",
            usage=Usage(input_tokens=10, output_tokens=10,
                        cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

    mock.helpers = type("H", (), {
        "make_message": staticmethod(make_message),
        "text": staticmethod(lambda t: TextBlock(type="text", text=t)),
        "tool_use": staticmethod(lambda name, args, id="tu_1":
            ToolUseBlock(type="tool_use", id=id, name=name, input=args)),
    })
    return mock


class _AsyncIterPage:
    """Async iterator usable in place of arcadepy's AsyncPaginator."""
    def __init__(self, items): self._items = items
    def __aiter__(self): self._i = iter(self._items); return self
    async def __anext__(self):
        try: return next(self._i)
        except StopIteration: raise StopAsyncIteration
```

Notes for the implementer:

- The `_AsyncIterPage` shim is fine for v1 — `arcadepy.list` returns an
  `AsyncPaginator` that is itself directly `async`-iterable.
- The `Usage` constructor fields may differ across `anthropic` SDK versions; if the
  test setup fails, fill in whatever the installed SDK requires.

### 7.4 Live smoke probe — `scripts/probe.py`

Not a pytest test. Run manually after keys land:

```
$ ARCADE_API_KEY=... ANTHROPIC_API_KEY=... .venv/bin/python scripts/probe.py
```

Required behavior:

- Loads `.env` via `dotenv.load_dotenv()`.
- Constructs `AsyncArcade(api_key=os.environ["ARCADE_API_KEY"])`.
- Calls `tools.list(toolkit="Gmail", include_format=["anthropic"], limit=100)` and
  `tools.list(toolkit="GoogleCalendar", include_format=["anthropic"], limit=100)`,
  iterating the paginator.
- Prints, for each tool:
  ```
  <qualified_name>
    description: <first 80 chars>
    formatted_schema.name: <value>
    requirements.authorization.provider_id: <value>
  ```
- Exits 0 if both lists are non-empty; 1 otherwise with a clear error.
- Wrap the whole thing in `if __name__ == "__main__": asyncio.run(main())`.

This script is permanent (not deleted) so future contributors can re-run it after
an Arcade API change.

---

## 8. Implementation order & acceptance gates

Implement in this order. Do not start the next module until the previous one's
tests are green.

1. **`errors.py`** — write the exception classes. Acceptance: `pytest tests/test_errors.py -x` (one short test file asserting type hierarchy + attributes).
2. **`bindings.py`** — implement + write `test_bindings.py`. Acceptance: all unit tests pass; lint clean.
3. **`arcade_client.py`** — implement + write `test_arcade_client.py`. Acceptance: every method has a happy + at least one sad-path test.
4. **`confirm.py`** — implement + write `test_confirm.py`. Acceptance: snapshot tests pass.
5. **`claude_agent.py`** — implement + write `test_claude_agent.py`. Acceptance: at minimum the six scenarios listed in §7.1 pass.
6. **`daemon.py`** — implement + write `test_daemon.py`. Acceptance: §7.2 integration scenarios pass on a real UDS.
7. **`cli.py`** — replace stub + write `test_cli.py`. Acceptance: `arcade-agent --help` lists every command; `pytest tests/test_cli.py` green.
8. **`scripts/probe.py`** — implement and run manually once real keys exist.

After step 7, the demo `arcade-agent init` → `arcade-agent daemon start` →
`arcade-agent ask "list my last 3 emails"` must work end-to-end against live Arcade
+ Anthropic.

---

**End of spec.** Anything not specified here that the implementer must decide
should first be raised back to the architect (i.e. the maintainer of this file)
rather than chosen silently.
