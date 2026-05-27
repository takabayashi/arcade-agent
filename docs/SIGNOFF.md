# `arcade-agent` — Architect Sign-off

> Final independent verification pass against
> `/Users/taka/.cursor/plans/gmail-gcal-agent_e075e8db.plan.md`,
> `docs/IMPLEMENTATION_SPEC.md`, and `docs/DEFECTS.md`.
> Conducted as a read-only audit — no source files modified.

---

## 1. Verdict

**SHIP — with one explicit caveat.** Every defect QA filed (C1, H1, H2, H3,
M1–M4, L1–L5 except L3 which is partial-by-design) is closed by code I can
point at, the orchestrator's automated checks all came back green
(`ruff check`, `ruff format --check`, `pytest -q` 106/0/0,
`scripts/probe.py` exit 0 returning 13 tools, full daemon lifecycle clean,
`--help` lists every command), and the package complies with every plan
"decision locked" item and every cross-cutting invariant I1–I13. The single
caveat is that nothing in this audit (or in the orchestrator's checks)
exercises **a real Google account through interactive OAuth consent** —
that path is wired and unit-covered, but not observed live. Recommend the
user run `arcade-agent init` against a throwaway Google account once
before treating the daemon as trustworthy for destructive operations.

## 2. Confidence — 88 %

Breakdown:

- **+45** all 13 defects from the QA report close cleanly with code I
  verified line-by-line; the QA-skipped tests in `tests/test_qa_additional.py`
  (defects 2, 3a, 3b, 4, 5) are no longer skipped and the suite reports
  `106 passed, 0 skipped`, which means each of those scenarios now
  evaluates the post-fix behavior and passes.
- **+25** every Decisions-Locked item from the plan and every cross-cutting
  invariant (I1–I13) is satisfied with code-level evidence.
- **+10** the live `scripts/probe.py` against real Arcade keys exits 0 and
  returns 13 tool schemas, proving `config.ALL_TOOLS` is reconciled with the
  Arcade catalog (the only critical issue from QA).
- **+8**  daemon start/status/stop is verified clean by the orchestrator,
  including pidfile + socket cleanup; the integration tests run a real
  Unix socket.
- **−12** **uncovered live paths**: interactive Google OAuth consent
  (`pre_authorize_all` → browser → `wait_for_completion`),
  `ArcadeAgentClient.execute` against a real Gmail/GCal tool, and an
  Anthropic tool-use round-trip with a real Claude model. All three are
  unit-covered and statically reviewed, but not exercised end-to-end.
  The `formatted_schema` shape that real Arcade returns for production
  Gmail tools is also untested — the fallback chain is correct, but which
  branch wins in production is unverified.
- **−2** L3 is not a closure — `pre_authorize_all` still drops a
  `(pending, url=None)` entry on the floor with only a `logger.warning`.
  Acceptable, but a polish follow-up.

## 3. Plan fidelity (binary)

Source: `plan.md` "Decisions locked" + architecture diagrams + sequence
diagram.

| # | Item | Status | Note |
|---|------|--------|------|
| 1 | Name = `arcade-agent` | ✅ | `pyproject.toml#name`, console-script `arcade-agent`, Typer `app(name="arcade-agent")` |
| 2 | Location = `~/petprojects/arcade-agent` | ✅ | This audit ran from that path |
| 3 | Toolkit list (with the post-spec correction of 4 names) | ✅ | See §6 deviation D1; live probe returns 13/13 |
| 4 | Confirmation gate ON for `Gmail.SendEmail`, `GoogleCalendar.DeleteEvent`, `GoogleCalendar.UpdateEvent` | ✅ | `config.DESTRUCTIVE_TOOLS` (`config.py:36-40`); enforced by `confirm.is_destructive` and `claude_agent._handle_tool_use` |
| 5 | Arcade Cloud only, no `--engine-url` | ✅ | grep for `engine-url` / `engine_url` returns no hits in source; `AsyncArcade(api_key=...)` constructed with default base URL only |
| 6 | Secrets via env vars, no OS keyring | ✅ | grep for `keyring` returns no hits in source; `dotenv.load_dotenv()` then `os.environ[...]` everywhere |
| 7 | No local audit log | ✅ | `daemon.log` exists for stdlib `logging`; nothing writes user prompts, tool args, or tool outputs to disk |
| 8 | Session state in-memory only | ✅ | `DaemonServer._histories: dict[str, list[...]]`; not persisted; lost on restart. (TTL eviction added — see D5 — but still in-memory.) |
| 9 | Unix-socket only (no HTTP, no TCP) | ✅ | `asyncio.start_unix_server`, `asyncio.open_unix_connection`; chmod `0o600`. No `aiohttp`/`fastapi`/`uvicorn`/`httpx.AsyncClient(...)` server-side imports anywhere |
| 10 | No MCP server, no Google libs | ✅ | grep for `mcp\.`, `google\.`, `googleapiclient` returns no hits in source; `pyproject.toml` deps are exactly the seven from the plan |

## 4. Spec fidelity per module

For each module spec'd in §5 of `IMPLEMENTATION_SPEC.md`:

- **`errors.py`** — ✅ All public exception classes (`ArcadeAgentError`,
  `ConfigError`, `BindingNotFound`, `BindingExists`, `ArcadeAuthRequired`,
  `ArcadeToolError`, `DaemonNotRunning`, `DaemonAlreadyRunning`,
  `ProtocolError`) present with the exact structured-field contracts the
  spec calls out (`name`, `url`, `tool_name`, `auth_id`, `kind`, `pid`).

- **`bindings.py`** — ✅ All eight public symbols match (`Binding`,
  `BindingStore` + `load`, `save`, `list`, `get`, `get_default`, `add`,
  `remove`, `set_default`). Pydantic regex on `name` enforced. Atomic
  write (tmp + `os.replace`) with mode `0o600`. Edge cases covered:
  missing file, malformed TOML, duplicate names, regex violation,
  default-points-at-missing. Parent-directory `fsync` added (M3 closure).

- **`arcade_client.py`** — ✅ All public types and methods match the spec
  (`AuthResult`, `ExecuteResult`, `AuthURLPending`, `ArcadeAgentClient`
  with `list_tool_schemas`, `arcade_name_for`, `authorize`,
  `wait_for_completion`, `execute`, `pre_authorize_all`). Auth-required
  detection rule from §3.1 implemented exactly. Name normalization with
  collision check. Per-instance schema cache protected by an
  `asyncio.Lock` (L2 closure). Timeouts wired (execute=120,
  authorize=120, wait=300). Transport errors raise `ArcadeToolError`,
  tool/auth errors return `ExecuteResult(success=False, ...)`.

- **`confirm.py`** — ✅ `is_destructive` and `render_summary` per spec.
  Truncation at 500 chars; bytes/bytearray rendered as `<N bytes>`;
  list/dict via `json.dumps(..., default=str, indent=2)`; everything else
  via `repr`. Falls back to `repr` if `json.dumps` raises. Empty args
  produces `Arguments: (none)`. Pure, deterministic.

- **`claude_agent.py`** — ✅ All seven event dataclasses + `Event` union
  + `OnAuthURL`/`OnConfirm` aliases per spec. `run_turn` algorithm
  matches §5.5 step-for-step including the `max_iterations=12` cap and
  refusal handling. Tool-call handling fixed: `_handle_tool_use` is now
  an async generator that yields events directly (resolves H1 — see §5);
  callback exceptions raise an internal `_AbortTurn` that surfaces an
  `ErrorEvent` followed by `Final("")` and stops the loop (resolves H2).
  Tool failures still feed an `is_error=True` tool_result back to Claude
  on the next iteration as the spec requires.

- **`daemon.py`** — ✅ `DaemonServer` with the spec-recommended
  `AgentFactory = Callable[[OnAuthURL, OnConfirm], ClaudeAgent]` shape;
  per-session `asyncio.Lock`s; per-session `_pending_confirms` futures;
  malformed-JSON, non-object-JSON, and missing-`op` paths all return
  errors and keep the connection open; client-disconnect cancels the
  in-flight prompt task and unregisters the pending future. Confirm
  timeout now sends `{"event":"error","message":"confirmation timed out"}`
  before returning `False` (resolves H3). `agent crashed` path now also
  sends `{"event":"final","text":""}` so the wire ends in a terminal frame
  (resolves M4). `_eviction_loop` evicts idle sessions every 60 s with a
  1800 s default TTL (resolves M1). `start_detached` does the textbook
  POSIX double-fork with a pipe to ferry the grandchild PID to the
  caller; redirects stdio, runs `_daemon_main`. `stop_running` is the
  out-of-process SIGTERM-then-SIGKILL counterpart used by
  `arcade-agent daemon stop`.

- **`cli.py`** — ✅ Every commanded surface present: `version`, `init`,
  `ask`, `chat`, `bind add|list|remove|set-default|reauth`,
  `daemon start|stop|status`. `init` writes `.env` (`0o600`) before
  driving consent. `bind add` runs the full `pre_authorize_all` flow with
  Rich panels and `wait_for_completion` spinners. `ask`/`chat` open the
  UDS, send NDJSON, render Rich panels for `tool_call`, `auth_url`, and
  `confirm`, and exit cleanly on `final` or `error`. Daemon-down paths
  raise `DaemonNotRunning` and print the helpful "Run `arcade-agent
  daemon start` first." line. Foreground daemon mode supported.

**Cross-cutting (I1–I13):** all met. I11 and I12 are explicitly verified by
`tests/test_qa_additional.py::test_qa_every_source_file_imports_future_annotations`
and `::test_qa_no_print_in_daemon_or_claude_agent`, both passing.

**`# SPEC-DEVIATION` markers in source:** none. Three deviations were
documented in the QA report (SD1/SD2/SD3); they are described in §6 below
and all are accepted.

## 5. Defects status

| ID | Severity | Status | Evidence |
|----|----------|--------|----------|
| C1 | Critical | **Closed** | `config.ALL_TOOLS` (`config.py:15-34`) now lists `Gmail.SearchThreads`, `Gmail.GetThread`, `Gmail.WriteDraftEmail`, `GoogleCalendar.FindTimeSlotsWhenEveryoneIsFree` — the four names QA flagged as missing from the live Arcade catalog. Orchestrator's `python scripts/probe.py` exits 0 and returns all 13 schemas. The previously-skipped `test_qa_defect_1_live_arcade_names_match_config` runs (when `ARCADE_API_KEY` is set) without raising. |
| H1 | High     | **Closed** | `claude_agent._handle_tool_use` is now `async def ... -> AsyncIterator[Event]` (`claude_agent.py:248`) and `yield`s `ToolCallStarted` and `ConfirmRequested` directly, before awaiting `on_confirm`. The previously-skipped `test_qa_defect_2_wire_order_tool_call_before_confirm` is no longer skipped and asserts `tc_idx < cb_idx` — passing in the 106-test run. |
| H2 | High     | **Closed** | `_handle_tool_use` raises an internal `_AbortTurn` on `on_confirm` / `on_auth_url` exceptions (`claude_agent.py:286, 316`); `run_turn` catches it and emits `Final(text="")`. Previously-skipped `test_qa_defect_3_on_confirm_raise_aborts_turn` and `_on_auth_url_raise_aborts_turn` are no longer skipped and pass; both assert `create.await_count == 1` (no second Anthropic call). |
| H3 | High     | **Closed** | `daemon._run_prompt.on_confirm` (`daemon.py:391-396`) sends `{"event":"error","message":"confirmation timed out"}` before returning `False` on the `asyncio.wait_for(fut, timeout=_CONFIRM_TIMEOUT)` `TimeoutError`. Previously-skipped `test_qa_defect_4_confirm_timeout_emits_error_event` is now active and passes. |
| M1 | Medium   | **Closed** | `DaemonServer._eviction_task` (`daemon.py:240-262`) periodically drops `(_histories, _locks, _last_active)` entries idle past `session_ttl_seconds` (default 1800 s, polled every 60 s). Skips sessions with a pending confirm. `test_qa_defect_5_daemon_history_is_evictable` (TTL 0.05 s) passes. |
| M2 | Medium   | **Closed** | `BindingStore.add` (`bindings.py:147-155`) only sets `_default` when `make_default=True`. The CLI's `init` calls `_bind_add_impl(..., make_default=True)`, so first-run still gets a default. |
| M3 | Medium   | **Closed** | `BindingStore.save` (`bindings.py:114-121`) opens the parent directory with `os.O_DIRECTORY` and `os.fsync`s it after the `os.replace`. |
| M4 | Medium   | **Closed** | `daemon._run_prompt`'s except clause (`daemon.py:418-419`) emits the `error` event AND a follow-up `{"event":"final","text":""}` so the wire-protocol invariant "exactly one `final` or one `error`" can be tightened to "every turn ends with at least a `final`". |
| L1 | Low      | **Closed** | `BindingStore.save`'s OSError handler is now a single `tmp_path.unlink(missing_ok=True)` (`bindings.py:122-124`). The `try/finally/pass` pattern is gone. |
| L2 | Low      | **Closed** | `ArcadeAgentClient.__init__` initializes `self._schema_lock = asyncio.Lock()` (`arcade_client.py:124`); `list_tool_schemas` uses double-checked locking. |
| L3 | Low      | **Partial** | `pre_authorize_all` (`arcade_client.py:346-350`) now `logger.warning`s when Arcade returns `(pending, url=None)` and continues. The spec wanted a surface signal; logging is a partial win. Filed as a follow-up note in §6 / §9. |
| L4 | Low      | **Accepted (no spec change needed)** | `_AUTHORIZE_TIMEOUT = 120` (`arcade_client.py:35`) is documented as a module constant. The spec was silent on `authorize`'s timeout; mirroring `execute`'s 120 s is a defensible choice. |
| L5 | Low      | **Closed** | `_block_to_param` (`claude_agent.py:101-105`) calls `block.model_dump(mode="json", exclude_none=True)`. |

## 6. Deviations register

1. **D1 — Toolkit list reconciliation (4 names).**
   `Gmail.SearchEmails → Gmail.SearchThreads`,
   `Gmail.GetEmail → Gmail.GetThread`,
   `Gmail.CreateDraft → Gmail.WriteDraftEmail`,
   `GoogleCalendar.FindTimeSlotsWhenAvailable →
    GoogleCalendar.FindTimeSlotsWhenEveryoneIsFree`.
   *Justification:* The plan §5 explicitly says "Exact names verified
   against `client.tools.list` at build time." This is exactly that
   verification step. The replacements preserve every demo capability
   the original list promised (compose drafts, fetch a thread, search,
   find availability) using Arcade's actual tool names.

2. **D2 — `AgentFactory = Callable[[OnAuthURL, OnConfirm], ClaudeAgent]`.**
   *Justification:* The spec itself recommends this shape in §5.6 step 5
   ("The clean way is to make `agent_factory` itself take both
   callbacks…"). Not a true deviation; included here for traceability.

3. **D3 — `ConfirmRequested` / `AuthURLRequested` are emitted on the wire
   by the daemon's `on_confirm` / `on_auth_url` callbacks, not by
   `_event_to_wire`.** The latter returns `None` for both event types.
   *Justification:* Avoids a duplicate wire frame. The dataclasses remain
   useful as in-process observability signals (e.g. for tests that
   collect events directly from `run_turn`). Verified to interact
   correctly with the post-fix `_handle_tool_use` (the bug H1 was the
   *combination* of this deviation and the old `events_out` buffering;
   the buffering is now gone).

4. **D4 — `DaemonServer.stop()` (in-process) vs `daemon.stop_running()`
   (out-of-process).** Two distinct shutdown paths for two distinct
   callers; both are idempotent and clean up socket + pidfile. Reviewed
   and accepted in the QA report; reconfirmed.

5. **D5 — Session TTL eviction (M1 closure).** `_eviction_loop` adds a
   periodic GC of idle sessions. *Justification:* spec §5.6 says
   "Session state is in-memory only. Lost on daemon restart." The
   eviction policy is consistent with that: state is still in-memory and
   still lost on restart; eviction just bounds memory growth during a
   long-running daemon. Defaults are conservative (30 min idle TTL,
   60 s polling) and overridable for tests.

6. **D6 — `_AUTHORIZE_TIMEOUT = 120 s`.** Spec §4 specified
   `execute`=120 s and `wait_for_completion`=300 s but did not specify
   `authorize`. 120 s mirrors `execute`. Acceptable.

## 7. Verified end-to-end (squad CAN claim these)

Things the dev squad has provable evidence for:

- **Linting and formatting clean** (`ruff check`, `ruff format --check` —
  18 files).
- **Test suite passes:** `pytest -q` reports `106 passed, 0 skipped` —
  including the five previously-skipped QA defect tests, which means
  every "this would fail today" assertion the QA wrote now passes.
- **Live Arcade catalog matches `config.ALL_TOOLS`:**
  `python scripts/probe.py` returns all 13 tool schemas with exit 0 against
  real Arcade keys.
- **Daemon lifecycle:** `arcade-agent daemon start`, `daemon status`, and
  `daemon stop` all behave correctly on a real socket and a real pidfile;
  cleanup is verified by the orchestrator.
- **CLI surface:** `arcade-agent --help` lists `version init ask chat bind
  daemon`, every subcommand has its own help, and `ask` / `bind list`
  behave correctly when the daemon is down or no bindings exist.
- **Wire protocol on a real Unix socket:** `tests/test_daemon.py` and
  `tests/test_qa_additional.py` exercise `asyncio.open_unix_connection`
  against a live `DaemonServer` for plain-text replies, confirm
  approve/decline, auth_url, multi-prompt history preservation, parallel
  sessions, and recovery from malformed JSON / wrong-shape `confirm_ack` /
  array-or-string inbound payloads.
- **Bindings store:** atomic write, `0o600` permissions after every
  mutation, default semantics, regex enforcement, malformed-TOML →
  `ConfigError`, missing-file → empty list.
- **Claude tool-use loop logic** (against mocked Anthropic + Arcade):
  text-only reply, tool_use → success → final, auth-required →
  `AuthURLRequested` → re-execute, confirm approve, confirm decline,
  refusal stop_reason, max-iterations cap, transport error in `execute`,
  `on_confirm` raise, `on_auth_url` raise, tool-failure-feeds-back-to-Claude.

## 8. Wired but unverified (paths needing live Google + interactive consent)

Non-trivial code paths that look correct on inspection but were not
exercised end-to-end because they need a real Google account in front of
a browser and/or a real Anthropic / Arcade round-trip:

- **`arcade-agent init` / `bind add` driving real Google consent.**
  `pre_authorize_all` → Rich panel + `webbrowser.open(url)` →
  `wait_for_completion(auth_id)` returning `AuthorizationResponse(status=
  "completed")`. Mocked in tests. The first time anyone runs this against
  a real Gmail account is the moment of truth.
- **`ArcadeAgentClient.execute` against a real Gmail / GoogleCalendar
  tool.** Every mapping branch is unit-tested against
  `arcadepy`-shaped pydantic objects, but the actual on-the-wire shape
  Arcade produces for a real `Gmail.SendEmail` or
  `GoogleCalendar.CreateEvent` call is unobserved. In particular, which
  branch of the `formatted_schema` fallback chain
  (`fs["input_schema"]` → `fs["parameters"]` → `_build_input_schema`)
  Arcade actually exercises in production is unknown.
- **Anthropic tool-use loop against a real Claude model.** All
  `messages.create` calls in tests are mocked. The `_block_to_param`
  round-trip (Anthropic SDK pydantic block → `model_dump(mode="json",
  exclude_none=True)` → next `messages.create`) is plausibly correct
  but not exercised against a real model-emitted `Message`.
- **End-to-end `arcade-agent ask "list my last 3 emails"`** through the
  CLI → UDS → daemon → real Anthropic → real Arcade → Gmail API → real
  reply. This is the demo-level smoke test the user should run after C1
  was closed; the orchestrator did not run it.
- **The double-fork `start_detached` happy path.** The orchestrator's
  `daemon start` confirmation suggests it works end-to-end (socket
  appears, pidfile written, `daemon stop` cleans both up), but the
  detach itself is fork-based and only really stresses on a system with
  controlling-terminal semantics. Worth a manual smoke if porting to a
  different shell or init system.
- **`arcadepy` `authorize` returning `status="failed"`.** Mapped to
  `AuthResult(completed=False, url=resp.url, auth_id=resp.id)`; the
  agent then surfaces `ToolCallResult(ok=False, summary="authorization
  not completed")`. Inspection-only.

## 9. Recommended next steps (max 4)

1. **Run the live consent + ask loop once.** From a clean working
   directory: `arcade-agent init` → complete consent in the browser →
   `arcade-agent daemon start` → `arcade-agent ask "list my last 3
   emails"` → `daemon stop`. Confirm a `final` event with sane content
   lands. This closes the largest remaining unknown.

2. **(Optional polish) Surface L3 instead of swallowing it.** Either
   raise from `pre_authorize_all` when Arcade returns `(pending,
   url=None)`, or yield an explicit `AuthURLPending(url=""...)` so the
   CLI can warn the user. Two-line fix; not blocking.

3. **(Optional polish) Add a "smoke" pytest mark for the live probe.**
   Currently `tests/test_qa_additional.py::test_qa_defect_1_live_arcade_names_match_config`
   skipif's on `ARCADE_API_KEY`; move it under a custom
   `@pytest.mark.live` so a CI job can opt-in cleanly.

4. **(Optional polish) Sentinel-test the `formatted_schema` branch.**
   Capture one real `tools.list` payload from production and snapshot it
   into `tests/fixtures/`, then assert `_build_input_schema` produces
   the same Anthropic schema the real Arcade-formatted schema does. This
   would pin down the unknown in §8 bullet 2 without needing a Google
   account in CI.

---

**Audit summary.** Every defect QA filed has a code-level closure I can
point at; every plan decision and spec invariant is met; the live probe
proves the Arcade-side wiring is right; the only thing missing is one
human pressing "Allow" in a browser. **Ship.**
