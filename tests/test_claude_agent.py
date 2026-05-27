"""Unit tests for arcade_agent.claude_agent.ClaudeAgent."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from arcade_agent.arcade_client import ArcadeAgentClient, ExecuteResult
from arcade_agent.claude_agent import (
    AuthURLRequested,
    ClaudeAgent,
    ConfirmRequested,
    ErrorEvent,
    Final,
    TextDelta,
    ToolCallResult,
    ToolCallStarted,
)


def _make_agent_client(mock_arcade: MagicMock, mapping: dict[str, str]) -> ArcadeAgentClient:
    client = ArcadeAgentClient(mock_arcade, list(mapping.values()))
    client._anthropic_to_arcade = dict(mapping)
    client._arcade_to_anthropic = {v: k for k, v in mapping.items()}
    client._schemas = []
    return client


async def _drain(agent: ClaudeAgent, history: list[dict[str, Any]], prompt: str = "hi") -> list:
    return [ev async for ev in agent.run_turn("user@x", prompt, history)]


async def test_text_only_response(mock_arcade: MagicMock, mock_anthropic: MagicMock) -> None:
    arcade_client = _make_agent_client(mock_arcade, {})
    msg = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Hello!"), stop_reason="end_turn"
    )
    mock_anthropic.messages.create = AsyncMock(return_value=msg)

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=AsyncMock(return_value=True),
    )

    events = await _drain(agent, history=[])
    assert any(isinstance(e, TextDelta) and e.text == "Hello!" for e in events)
    final = [e for e in events if isinstance(e, Final)]
    assert len(final) == 1
    assert final[0].text == "Hello!"


async def test_tool_use_then_final(mock_arcade: MagicMock, mock_anthropic: MagicMock) -> None:
    arcade_client = _make_agent_client(mock_arcade, {"Gmail_ListEmails": "Gmail.ListEmails"})
    mock_arcade.tools.execute = AsyncMock(
        return_value=MagicMock(success=True, output=MagicMock(value={"messages": ["m1"]}))
    )
    arcade_client.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecuteResult(
            success=True,
            output={"messages": ["m1"]},
            auth_url=None,
            error_message=None,
            error_kind=None,
        )
    )

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_ListEmails", {"max_results": 5}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Here are your emails."), stop_reason="end_turn"
    )
    mock_anthropic.messages.create = AsyncMock(side_effect=[msg1, msg2])

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=AsyncMock(return_value=True),
    )
    events = await _drain(agent, history=[])

    assert any(isinstance(e, ToolCallStarted) and e.name == "Gmail.ListEmails" for e in events)
    assert any(isinstance(e, ToolCallResult) and e.ok for e in events)
    final = [e for e in events if isinstance(e, Final)]
    assert final and final[-1].text == "Here are your emails."


async def test_destructive_tool_confirm_approve(
    mock_arcade: MagicMock, mock_anthropic: MagicMock
) -> None:
    arcade_client = _make_agent_client(mock_arcade, {"Gmail_SendEmail": "Gmail.SendEmail"})
    arcade_client.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecuteResult(
            success=True, output={"id": "x"}, auth_url=None, error_message=None, error_kind=None
        )
    )
    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_SendEmail", {"to": "a@b"}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Sent."), stop_reason="end_turn"
    )
    mock_anthropic.messages.create = AsyncMock(side_effect=[msg1, msg2])

    on_confirm = AsyncMock(return_value=True)
    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=on_confirm,
    )
    events = await _drain(agent, history=[])
    on_confirm.assert_awaited_once()
    assert any(isinstance(e, ConfirmRequested) for e in events)
    assert any(isinstance(e, ToolCallResult) and e.ok for e in events)
    assert isinstance(events[-1], Final)


async def test_destructive_tool_confirm_deny(
    mock_arcade: MagicMock, mock_anthropic: MagicMock
) -> None:
    arcade_client = _make_agent_client(mock_arcade, {"Gmail_SendEmail": "Gmail.SendEmail"})
    arcade_client.execute = AsyncMock()  # type: ignore[method-assign]

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_SendEmail", {"to": "a@b"}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Acknowledged."), stop_reason="end_turn"
    )
    mock_anthropic.messages.create = AsyncMock(side_effect=[msg1, msg2])

    on_confirm = AsyncMock(return_value=False)
    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=on_confirm,
    )
    events = await _drain(agent, history=[])
    arcade_client.execute.assert_not_awaited()
    declined = [e for e in events if isinstance(e, ToolCallResult)]
    assert declined and declined[0].ok is False and declined[0].summary == "user declined"


async def test_auth_required_then_retry_succeeds(
    mock_arcade: MagicMock, mock_anthropic: MagicMock
) -> None:
    arcade_client = _make_agent_client(mock_arcade, {"Gmail_ListEmails": "Gmail.ListEmails"})

    arcade_client.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            ExecuteResult(
                success=False,
                output=None,
                auth_url="https://consent",
                error_message=None,
                error_kind="AUTH_REQUIRED",
            ),
            ExecuteResult(
                success=True,
                output={"ok": True},
                auth_url=None,
                error_message=None,
                error_kind=None,
            ),
        ]
    )

    from arcade_agent.arcade_client import AuthResult

    arcade_client.authorize = AsyncMock(  # type: ignore[method-assign]
        return_value=AuthResult(completed=True, url=None, auth_id="auth_x")
    )
    arcade_client.wait_for_completion = AsyncMock()  # type: ignore[method-assign]

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_ListEmails", {}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Done."), stop_reason="end_turn"
    )
    mock_anthropic.messages.create = AsyncMock(side_effect=[msg1, msg2])

    on_auth = AsyncMock()
    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=on_auth,
        on_confirm=AsyncMock(return_value=True),
    )
    events = await _drain(agent, history=[])
    on_auth.assert_awaited_once_with("https://consent")
    assert any(isinstance(e, AuthURLRequested) for e in events)
    assert any(isinstance(e, ToolCallResult) and e.ok for e in events)
    assert arcade_client.execute.await_count == 2


async def test_refusal_stop_reason(mock_arcade: MagicMock, mock_anthropic: MagicMock) -> None:
    arcade_client = _make_agent_client(mock_arcade, {})
    msg = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Sorry."), stop_reason="refusal"
    )
    mock_anthropic.messages.create = AsyncMock(return_value=msg)

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=AsyncMock(return_value=True),
    )
    events = await _drain(agent, history=[])
    assert any(isinstance(e, ErrorEvent) and "refused" in e.message for e in events)
    assert isinstance(events[-1], Final)


async def test_max_iterations_cap(mock_arcade: MagicMock, mock_anthropic: MagicMock) -> None:
    arcade_client = _make_agent_client(mock_arcade, {"Gmail_ListEmails": "Gmail.ListEmails"})
    arcade_client.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecuteResult(
            success=True, output={}, auth_url=None, error_message=None, error_kind=None
        )
    )

    looping = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_ListEmails", {}, id="tu_loop"),
        stop_reason="tool_use",
    )
    mock_anthropic.messages.create = AsyncMock(return_value=looping)

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=AsyncMock(return_value=True),
        max_iterations=3,
    )
    events = await _drain(agent, history=[])
    assert any(isinstance(e, ErrorEvent) and "max tool-use iterations" in e.message for e in events)
    assert isinstance(events[-1], Final)


async def test_transport_error_in_execute_does_not_crash(
    mock_arcade: MagicMock, mock_anthropic: MagicMock
) -> None:
    from arcade_agent.errors import ArcadeToolError

    arcade_client = _make_agent_client(mock_arcade, {"Gmail_ListEmails": "Gmail.ListEmails"})
    arcade_client.execute = AsyncMock(side_effect=ArcadeToolError("network down"))  # type: ignore[method-assign]

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Gmail_ListEmails", {}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Sorry."), stop_reason="end_turn"
    )
    mock_anthropic.messages.create = AsyncMock(side_effect=[msg1, msg2])

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=AsyncMock(return_value=True),
    )
    events = await _drain(agent, history=[])
    assert any(isinstance(e, ErrorEvent) and "network down" in e.message for e in events)
    assert isinstance(events[-1], Final)


async def test_unknown_tool_name_recovers(
    mock_arcade: MagicMock, mock_anthropic: MagicMock
) -> None:
    arcade_client = _make_agent_client(mock_arcade, {})

    msg1 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.tool_use("Bogus_Tool", {}, id="tu_1"),
        stop_reason="tool_use",
    )
    msg2 = mock_anthropic.helpers.make_message(
        mock_anthropic.helpers.text("Sorry."), stop_reason="end_turn"
    )
    mock_anthropic.messages.create = AsyncMock(side_effect=[msg1, msg2])

    agent = ClaudeAgent(
        mock_anthropic,
        arcade_client,
        model="claude-test",
        on_auth_url=AsyncMock(),
        on_confirm=AsyncMock(return_value=True),
    )
    events = await _drain(agent, history=[])
    assert any(isinstance(e, ErrorEvent) and "unknown tool name" in e.message for e in events)
    assert isinstance(events[-1], Final)
