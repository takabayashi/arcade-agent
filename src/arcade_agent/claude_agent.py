"""The Anthropic Claude tool-use loop.

This is the only module that talks to Anthropic. It iterates between Claude (which
chooses tools) and :class:`ArcadeAgentClient` (which runs them on Arcade) until the
model emits a non-``tool_use`` stop reason.

The class is constructed at the daemon edge with per-connection ``on_auth_url`` and
``on_confirm`` callbacks; the daemon uses those to surface OAuth URLs and gate
destructive tool calls behind a single user's y/N without blocking other sessions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arcade_agent.arcade_client import ArcadeAgentClient
from arcade_agent.confirm import is_destructive
from arcade_agent.errors import ArcadeToolError

if TYPE_CHECKING:
    import anthropic

logger = logging.getLogger(__name__)

_TOOL_OUTPUT_TRUNCATE_BYTES = 8 * 1024
_TOOL_OUTPUT_TRUNCATE_SUFFIX = " […truncated]"
_TOOL_RESULT_SUMMARY_LIMIT = 200


@dataclass(slots=True, frozen=True)
class TextDelta:
    text: str


@dataclass(slots=True, frozen=True)
class ToolCallStarted:
    name: str
    args: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ToolCallResult:
    name: str
    ok: bool
    summary: str


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


Event = (
    TextDelta
    | ToolCallStarted
    | ToolCallResult
    | AuthURLRequested
    | ConfirmRequested
    | Final
    | ErrorEvent
)


OnAuthURL = Callable[[str], Awaitable[None]]
OnConfirm = Callable[[str, dict[str, Any]], Awaitable[bool]]


def _stringify_tool_output(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(value)
    if len(text.encode("utf-8")) > _TOOL_OUTPUT_TRUNCATE_BYTES:
        text = text[:_TOOL_OUTPUT_TRUNCATE_BYTES] + _TOOL_OUTPUT_TRUNCATE_SUFFIX
    return text


def _block_to_param(block: Any) -> dict[str, Any]:
    """Convert a Message.content block back to a MessageParam dict for Anthropic."""
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return dict(block)


def _tool_result_dict(tool_use_id: str, content: str, *, is_error: bool) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


class _AbortTurn(Exception):
    """Internal signal that a callback raised and the turn must be aborted.

    Per spec §5.5, ``on_confirm`` and ``on_auth_url`` exceptions terminate the
    turn after an :class:`ErrorEvent`. Raising this lets ``_handle_tool_use``
    surface the error event to the caller before unwinding ``run_turn`` so
    that no further Anthropic / Arcade work happens.
    """


class ClaudeAgent:
    """Stateless-per-call wrapper that runs one user turn against Claude+Arcade."""

    def __init__(
        self,
        anthropic_client: anthropic.AsyncAnthropic,
        arcade_client: ArcadeAgentClient,
        model: str,
        on_auth_url: OnAuthURL,
        on_confirm: OnConfirm,
        *,
        max_tokens: int = 4096,
        max_iterations: int = 12,
        system_prompt: str | None = None,
    ) -> None:
        self._anthropic = anthropic_client
        self._arcade = arcade_client
        self._model = model
        self._on_auth_url = on_auth_url
        self._on_confirm = on_confirm
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._system_prompt = system_prompt
        self._tools_cache: list[dict[str, Any]] | None = None

    async def _get_tools(self) -> list[dict[str, Any]]:
        if self._tools_cache is None:
            self._tools_cache = await self._arcade.list_tool_schemas()
        return self._tools_cache

    async def run_turn(
        self,
        user_id: str,
        prompt: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Event]:
        """Run a single user prompt to completion, yielding :class:`Event` values.

        ``history`` is mutated in place: subsequent calls with the same list continue
        the conversation. The terminal yield is always either :class:`Final` or an
        :class:`ErrorEvent` followed by :class:`Final`.
        """
        try:
            tools = await self._get_tools()
        except Exception as exc:
            yield ErrorEvent(f"failed to load tools: {exc}")
            yield Final(text="")
            return

        history.append({"role": "user", "content": prompt})

        for _iteration in range(self._max_iterations):
            create_kwargs: dict[str, Any] = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "messages": history,
                "tools": tools,
            }
            if self._system_prompt:
                create_kwargs["system"] = self._system_prompt

            try:
                resp = await self._anthropic.messages.create(**create_kwargs)
            except Exception as exc:
                logger.exception("anthropic.messages.create failed")
                yield ErrorEvent(f"anthropic call failed: {exc}")
                yield Final(text="")
                return

            usage = getattr(resp, "usage", None)
            if usage is not None:
                logger.debug(
                    "claude usage: input=%s output=%s",
                    getattr(usage, "input_tokens", None),
                    getattr(usage, "output_tokens", None),
                )

            history.append(
                {
                    "role": "assistant",
                    "content": [_block_to_param(block) for block in resp.content],
                }
            )

            tool_results: list[dict[str, Any]] = []
            text_chunks: list[str] = []

            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    text = getattr(block, "text", "") or ""
                    text_chunks.append(text)
                    if text:
                        yield TextDelta(text=text)
                elif btype == "tool_use":
                    try:
                        async for ev in self._handle_tool_use(user_id, block, tool_results):
                            yield ev
                    except _AbortTurn:
                        yield Final(text="")
                        return

            stop_reason = getattr(resp, "stop_reason", None)

            if stop_reason == "tool_use":
                if not tool_results:
                    yield ErrorEvent("model requested tool_use but emitted no tool blocks")
                    yield Final(text="".join(text_chunks))
                    return
                history.append({"role": "user", "content": tool_results})
                continue

            final_text = "".join(text_chunks)
            if stop_reason == "refusal":
                yield ErrorEvent("model refused to answer")
            yield Final(text=final_text)
            return

        yield ErrorEvent("hit max tool-use iterations")
        yield Final(text="")

    async def _handle_tool_use(
        self,
        user_id: str,
        block: Any,
        tool_results: list[dict[str, Any]],
    ) -> AsyncIterator[Event]:
        """Run one tool_use block as an async generator.

        Events are yielded directly so wire-protocol order matches the spec
        algorithm in §5.5 (yield ``ToolCallStarted`` and ``ConfirmRequested``
        BEFORE the await on ``on_confirm`` runs the actual confirm round-trip).
        The synthesized tool_result dict is appended to ``tool_results`` rather
        than returned so the generator stays a clean ``AsyncIterator[Event]``.
        Raises :class:`_AbortTurn` to signal that ``run_turn`` must terminate
        immediately with a ``Final("")`` and skip any further Anthropic /
        Arcade calls.
        """
        tool_use_id: str = getattr(block, "id", "")
        anthropic_name: str = getattr(block, "name", "")
        args: dict[str, Any] = getattr(block, "input", {}) or {}

        try:
            arcade_name = self._arcade.arcade_name_for(anthropic_name)
        except KeyError:
            message = f"unknown tool name from model: {anthropic_name!r}"
            yield ErrorEvent(message)
            tool_results.append(_tool_result_dict(tool_use_id, message, is_error=True))
            return

        yield ToolCallStarted(name=arcade_name, args=args)

        if is_destructive(arcade_name):
            yield ConfirmRequested(tool_name=arcade_name, args=args)
            try:
                approved = await self._on_confirm(arcade_name, args)
            except Exception as exc:
                logger.exception("on_confirm callback raised")
                yield ErrorEvent(f"on_confirm callback failed: {exc}")
                raise _AbortTurn() from exc
            if not approved:
                yield ToolCallResult(name=arcade_name, ok=False, summary="user declined")
                tool_results.append(
                    _tool_result_dict(
                        tool_use_id, f"User declined to run {arcade_name}.", is_error=True
                    )
                )
                return

        try:
            result = await self._arcade.execute(user_id, arcade_name, args)
        except ArcadeToolError as exc:
            yield ErrorEvent(exc.message)
            tool_results.append(_tool_result_dict(tool_use_id, str(exc), is_error=True))
            return

        if result.success:
            tool_result = self._build_success_result(tool_use_id, result.output)
            summary = tool_result["content"][:_TOOL_RESULT_SUMMARY_LIMIT]
            yield ToolCallResult(name=arcade_name, ok=True, summary=summary)
            tool_results.append(tool_result)
            return

        if result.auth_url is not None:
            try:
                await self._on_auth_url(result.auth_url)
            except Exception as exc:
                logger.exception("on_auth_url callback raised")
                yield ErrorEvent(f"on_auth_url callback failed: {exc}")
                raise _AbortTurn() from exc
            yield AuthURLRequested(url=result.auth_url)

            try:
                auth = await self._arcade.authorize(user_id, arcade_name)
                if not auth.completed:
                    if auth.auth_id is None:
                        raise ArcadeToolError(
                            "no auth_id returned for pending authorization",
                            tool_name=arcade_name,
                        )
                    await self._arcade.wait_for_completion(auth.auth_id)
            except ArcadeToolError as exc:
                yield ErrorEvent(exc.message)
                tool_results.append(_tool_result_dict(tool_use_id, str(exc), is_error=True))
                return

            try:
                retry = await self._arcade.execute(user_id, arcade_name, args)
            except ArcadeToolError as exc:
                yield ErrorEvent(exc.message)
                tool_results.append(_tool_result_dict(tool_use_id, str(exc), is_error=True))
                return

            if retry.success:
                tool_result = self._build_success_result(tool_use_id, retry.output)
                yield ToolCallResult(
                    name=arcade_name,
                    ok=True,
                    summary=tool_result["content"][:_TOOL_RESULT_SUMMARY_LIMIT],
                )
                tool_results.append(tool_result)
                return
            if retry.auth_url is not None:
                yield ToolCallResult(
                    name=arcade_name, ok=False, summary="authorization not completed"
                )
                tool_results.append(
                    _tool_result_dict(tool_use_id, "authorization not completed", is_error=True)
                )
                return
            error_text = retry.error_message or retry.error_kind or "tool failed"
            yield ToolCallResult(name=arcade_name, ok=False, summary=error_text)
            tool_results.append(_tool_result_dict(tool_use_id, error_text, is_error=True))
            return

        error_text = result.error_message or result.error_kind or "tool failed"
        yield ToolCallResult(name=arcade_name, ok=False, summary=error_text)
        tool_results.append(_tool_result_dict(tool_use_id, error_text, is_error=True))

    @staticmethod
    def _build_success_result(tool_use_id: str, output: Any) -> dict[str, Any]:
        text = _stringify_tool_output(output)
        return _tool_result_dict(tool_use_id, text, is_error=False)
