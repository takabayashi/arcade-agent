"""Thin async wrapper around `arcadepy.AsyncArcade` exposing just what the agent needs.

This is the only module in the package that talks to Arcade. It performs:

* Tool-schema discovery and a bidirectional name map between Arcade dotted names
  (e.g. ``Gmail.SendEmail``) and Anthropic-compatible underscore names
  (``Gmail_SendEmail``).
* Authorization: kicking off OAuth and waiting for completion.
* Tool execution with a uniform :class:`ExecuteResult` mapping that the agent layer
  consumes without ever needing to know about Arcade response shapes.

The wrapper never raises on tool-side or auth-side failures — those become
``ExecuteResult(success=False, ...)``. It only raises :class:`ArcadeToolError` on
transport, timeout, or malformed-response errors.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arcadepy import APIStatusError

from arcade_agent.errors import ArcadeToolError, ConfigError

if TYPE_CHECKING:
    import arcadepy

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_EXECUTE_TIMEOUT = 120
_AUTHORIZE_TIMEOUT = 120
_WAIT_TIMEOUT = 300


@dataclass(slots=True, frozen=True)
class AuthResult:
    completed: bool
    url: str | None
    auth_id: str | None


@dataclass(slots=True, frozen=True)
class ExecuteResult:
    success: bool
    output: Any
    auth_url: str | None
    error_message: str | None
    error_kind: str | None


@dataclass(slots=True, frozen=True)
class AuthURLPending:
    provider_id: str
    auth_id: str | None
    url: str


def _normalize_name(arcade_name: str) -> str:
    """Translate ``Gmail.SendEmail`` to ``Gmail_SendEmail`` (Anthropic-compatible)."""
    n = arcade_name.replace(".", "_")
    if not _NAME_RE.match(n):
        raise ConfigError(f"tool name not Anthropic-compatible after normalization: {n!r}")
    return n


def _is_tool_authorization_required(exc: APIStatusError) -> bool:
    """True iff Arcade raised the 403 that signals "user needs to OAuth this tool".

    Arcade's hosted API surfaces unmet tool authorization as HTTP 403 with body
    ``{"name": "tool_authorization_required", "message": "authorization required"}``
    rather than the spec-documented ``success=False`` payload, so we have to
    detect it on the exception object and route it into the AUTH_REQUIRED flow.
    """
    if exc.status_code != 403:
        return False
    body = exc.body
    if isinstance(body, dict) and body.get("name") == "tool_authorization_required":
        return True
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("name") == "tool_authorization_required":
            return True
    return False


_VAL_TYPE_TO_JSON: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "float": "number",
    "boolean": "boolean",
    "bool": "boolean",
    "array": "array",
    "object": "object",
    "json": "object",
}


def _build_input_schema(input_obj: Any) -> dict[str, Any]:
    """Construct an Anthropic-shaped JSON Schema from an Arcade ``Input`` model."""
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    parameters: Iterable[Any] = getattr(input_obj, "parameters", None) or []
    for param in parameters:
        name = param.name
        prop: dict[str, Any] = {}
        value_schema = param.value_schema
        val_type = (value_schema.val_type or "string").lower()
        json_type = _VAL_TYPE_TO_JSON.get(val_type, "string")
        prop["type"] = json_type
        inner = getattr(value_schema, "inner_val_type", None)
        if json_type == "array" and inner:
            inner_lower = inner.lower()
            prop["items"] = {"type": _VAL_TYPE_TO_JSON.get(inner_lower, "string")}
        if value_schema.enum:
            prop["enum"] = list(value_schema.enum)
        if param.description:
            prop["description"] = param.description
        properties[name] = prop
        if param.required:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


class ArcadeAgentClient:
    """Wrapper around :class:`arcadepy.AsyncArcade` tailored to this agent's needs."""

    def __init__(self, client: arcadepy.AsyncArcade, tool_names: list[str]) -> None:
        self._client = client
        self._tool_names: list[str] = list(tool_names)
        self._schemas: list[dict[str, Any]] | None = None
        self._arcade_to_anthropic: dict[str, str] = {}
        self._anthropic_to_arcade: dict[str, str] = {}
        self._schema_lock = asyncio.Lock()

    @staticmethod
    def _toolkit_of(arcade_name: str) -> str:
        toolkit, _, _ = arcade_name.partition(".")
        return toolkit

    async def list_tool_schemas(self) -> list[dict[str, Any]]:
        """Discover schemas for every tool in ``self._tool_names``.

        Idempotent: cached on the instance after the first successful call. Raises
        :class:`ConfigError` if a tool is missing on the Arcade side or if two tools
        normalize to the same Anthropic-compatible name.
        """
        if self._schemas is not None:
            return self._schemas

        async with self._schema_lock:
            if self._schemas is not None:
                return self._schemas

            wanted: set[str] = set(self._tool_names)
            toolkits: dict[str, list[str]] = {}
            for name in self._tool_names:
                toolkits.setdefault(self._toolkit_of(name), []).append(name)

            found: dict[str, Any] = {}
            try:
                for toolkit_name in toolkits:
                    paginator = self._client.tools.list(
                        toolkit=toolkit_name,
                        include_format=["anthropic"],
                        limit=100,
                    )
                    async for tool_def in paginator:
                        if tool_def.qualified_name in wanted:
                            found[tool_def.qualified_name] = tool_def
            except Exception as exc:  # pragma: no cover - transport-level
                raise ArcadeToolError(f"failed to list Arcade tools: {exc}") from exc

            missing = wanted - set(found)
            if missing:
                raise ConfigError(
                    "Arcade did not return the following configured tools: "
                    + ", ".join(sorted(missing))
                )

            schemas: list[dict[str, Any]] = []
            arcade_to_anthropic: dict[str, str] = {}
            anthropic_to_arcade: dict[str, str] = {}

            for arcade_name in self._tool_names:
                tool_def = found[arcade_name]
                anthropic_name = _normalize_name(arcade_name)
                if anthropic_name in anthropic_to_arcade:
                    raise ConfigError(
                        "tool name collision after normalization: "
                        f"{arcade_name!r} and {anthropic_to_arcade[anthropic_name]!r} "
                        f"both map to {anthropic_name!r}"
                    )
                arcade_to_anthropic[arcade_name] = anthropic_name
                anthropic_to_arcade[anthropic_name] = arcade_name

                description = tool_def.description or ""
                input_schema: dict[str, Any] | None = None
                if tool_def.formatted_schema:
                    fs = tool_def.formatted_schema
                    if isinstance(fs.get("input_schema"), dict):
                        input_schema = fs["input_schema"]
                    elif isinstance(fs.get("parameters"), dict):
                        input_schema = fs["parameters"]
                if input_schema is None:
                    input_schema = _build_input_schema(tool_def.input)

                schemas.append(
                    {
                        "name": anthropic_name,
                        "description": description,
                        "input_schema": input_schema,
                    }
                )

            self._schemas = schemas
            self._arcade_to_anthropic = arcade_to_anthropic
            self._anthropic_to_arcade = anthropic_to_arcade
            return schemas

    def arcade_name_for(self, anthropic_name: str) -> str:
        """Reverse-lookup an Anthropic tool name back to its Arcade dotted form."""
        if not self._anthropic_to_arcade:
            raise KeyError("tool name map not populated; call list_tool_schemas() first")
        try:
            return self._anthropic_to_arcade[anthropic_name]
        except KeyError as exc:
            raise KeyError(f"unknown anthropic tool name: {anthropic_name!r}") from exc

    async def authorize(self, user_id: str, tool_name: str) -> AuthResult:
        """Initiate (or fetch the status of) authorization for one Arcade tool."""
        try:
            resp = await asyncio.wait_for(
                self._client.tools.authorize(tool_name=tool_name, user_id=user_id),
                timeout=_AUTHORIZE_TIMEOUT,
            )
        except TimeoutError as exc:
            raise ArcadeToolError("arcade authorize timed out", tool_name=tool_name) from exc
        except ArcadeToolError:
            raise
        except Exception as exc:
            raise ArcadeToolError(f"arcade authorize failed: {exc}", tool_name=tool_name) from exc

        completed = resp.status == "completed"
        return AuthResult(
            completed=completed,
            url=None if completed else resp.url,
            auth_id=resp.id,
        )

    async def wait_for_completion(self, auth_id: str) -> None:
        """Block until consent finishes (or times out)."""
        try:
            resp = await asyncio.wait_for(
                self._client.auth.wait_for_completion(auth_id),
                timeout=_WAIT_TIMEOUT,
            )
        except TimeoutError as exc:
            raise ArcadeToolError("auth wait timed out") from exc
        except Exception as exc:
            raise ArcadeToolError(f"auth wait failed: {exc}") from exc

        if resp.status != "completed":
            raise ArcadeToolError(f"authorization did not complete (status={resp.status!r})")

    async def execute(
        self,
        user_id: str,
        tool_name: str,
        input_dict: dict[str, Any],
    ) -> ExecuteResult:
        """Run an Arcade tool and translate the response into :class:`ExecuteResult`.

        Maps tool-side and auth-side failures into the dataclass without raising.
        Only transport/timeout/malformed-response failures raise
        :class:`ArcadeToolError`.
        """
        try:
            resp = await asyncio.wait_for(
                self._client.tools.execute(
                    tool_name=tool_name,
                    user_id=user_id,
                    input=input_dict,
                ),
                timeout=_EXECUTE_TIMEOUT,
            )
        except TimeoutError as exc:
            raise ArcadeToolError("arcade execute timed out", tool_name=tool_name) from exc
        except ArcadeToolError:
            raise
        except APIStatusError as exc:
            if _is_tool_authorization_required(exc):
                logger.info(
                    "arcade signaled tool_authorization_required for %s; fetching consent URL",
                    tool_name,
                )
                fetched = await self.authorize(user_id, tool_name)
                return ExecuteResult(
                    success=False,
                    output=None,
                    auth_url=fetched.url,
                    error_message=None,
                    error_kind="AUTH_REQUIRED",
                )
            raise ArcadeToolError(
                f"arcade execute failed: {exc}", tool_name=tool_name
            ) from exc
        except Exception as exc:
            raise ArcadeToolError(f"arcade execute failed: {exc}", tool_name=tool_name) from exc

        if resp.output is None:
            raise ArcadeToolError("empty Arcade response", tool_name=tool_name)

        if resp.success is True:
            return ExecuteResult(
                success=True,
                output=resp.output.value,
                auth_url=None,
                error_message=None,
                error_kind=None,
            )

        authorization = resp.output.authorization
        error = resp.output.error
        auth_required = (authorization is not None and authorization.status != "completed") or (
            error is not None and error.kind == "TOOL_REQUIREMENTS_NOT_MET"
        )

        if auth_required:
            url: str | None = authorization.url if authorization is not None else None
            if url is None:
                fetched = await self.authorize(user_id, tool_name)
                url = fetched.url
            return ExecuteResult(
                success=False,
                output=None,
                auth_url=url,
                error_message=None,
                error_kind="AUTH_REQUIRED",
            )

        if error is None:
            raise ArcadeToolError(
                "arcade reported failure with no error payload", tool_name=tool_name
            )
        return ExecuteResult(
            success=False,
            output=None,
            auth_url=None,
            error_message=error.message,
            error_kind=error.kind,
        )

    async def pre_authorize_all(self, user_id: str) -> list[AuthURLPending]:
        """Drive a one-shot consent flow per toolkit, returning anything still pending."""
        if not self._tool_names:
            return []

        seen_toolkits: set[str] = set()
        representatives: list[str] = []
        for name in self._tool_names:
            tk = self._toolkit_of(name)
            if tk in seen_toolkits:
                continue
            seen_toolkits.add(tk)
            representatives.append(name)

        pending: list[AuthURLPending] = []
        for tool_name in representatives:
            result = await self.authorize(user_id, tool_name)
            if result.completed:
                continue
            if result.url is None:
                logger.warning(
                    "Arcade returned pending authorization for %s with no URL", tool_name
                )
                continue
            pending.append(
                AuthURLPending(
                    provider_id=self._toolkit_of(tool_name),
                    auth_id=result.auth_id,
                    url=result.url,
                )
            )
        return pending
