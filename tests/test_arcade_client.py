"""Unit tests for arcade_agent.arcade_client.ArcadeAgentClient."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcadepy.types.execute_tool_response import ExecuteToolResponse, Output, OutputError
from arcadepy.types.shared.authorization_response import AuthorizationResponse

from arcade_agent.arcade_client import (
    ArcadeAgentClient,
    AuthResult,
    AuthURLPending,
    ExecuteResult,
    _normalize_name,
)
from arcade_agent.errors import ArcadeToolError, ConfigError


def test_normalize_name_replaces_dot_with_underscore() -> None:
    assert _normalize_name("Gmail.SendEmail") == "Gmail_SendEmail"


def test_normalize_name_rejects_invalid() -> None:
    with pytest.raises(ConfigError):
        _normalize_name("Bad Name with spaces")


async def test_list_tool_schemas_builds_anthropic_dicts(
    mock_arcade: MagicMock,
    make_tool_definition: Any,
    async_iter_page: Any,
) -> None:
    tool_a = make_tool_definition(
        qualified_name="Gmail.SendEmail",
        description="Send an email",
        parameters=[
            {"name": "to", "type": "string", "required": True, "description": "recipient"},
            {"name": "body", "type": "string", "required": False},
        ],
    )
    tool_b = make_tool_definition(
        qualified_name="GoogleCalendar.ListEvents",
        description="List events",
        parameters=[{"name": "max_results", "type": "integer", "required": False}],
    )

    def list_side(**kwargs: Any) -> Any:
        toolkit = kwargs.get("toolkit")
        if toolkit == "Gmail":
            return async_iter_page([tool_a])
        if toolkit == "GoogleCalendar":
            return async_iter_page([tool_b])
        return async_iter_page([])

    mock_arcade.tools.list = MagicMock(side_effect=list_side)

    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail", "GoogleCalendar.ListEvents"])
    schemas = await client.list_tool_schemas()
    assert [s["name"] for s in schemas] == ["Gmail_SendEmail", "GoogleCalendar_ListEvents"]
    assert schemas[0]["description"] == "Send an email"
    assert schemas[0]["input_schema"]["type"] == "object"
    assert schemas[0]["input_schema"]["properties"]["to"] == {
        "type": "string",
        "description": "recipient",
    }
    assert schemas[0]["input_schema"]["required"] == ["to"]
    assert client.arcade_name_for("Gmail_SendEmail") == "Gmail.SendEmail"


async def test_list_tool_schemas_uses_formatted_schema_when_present(
    mock_arcade: MagicMock,
    make_tool_definition: Any,
    async_iter_page: Any,
) -> None:
    tool = make_tool_definition(
        qualified_name="Gmail.SendEmail",
        formatted_schema={
            "input_schema": {
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
            "name": "Gmail_SendEmail",
        },
    )

    mock_arcade.tools.list = MagicMock(return_value=async_iter_page([tool]))

    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    schemas = await client.list_tool_schemas()
    assert schemas[0]["input_schema"]["properties"]["to"] == {"type": "string"}
    assert schemas[0]["input_schema"]["required"] == ["to"]


async def test_list_tool_schemas_caches(
    mock_arcade: MagicMock,
    make_tool_definition: Any,
    async_iter_page: Any,
) -> None:
    tool = make_tool_definition(qualified_name="Gmail.SendEmail")
    mock_arcade.tools.list = MagicMock(return_value=async_iter_page([tool]))

    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    s1 = await client.list_tool_schemas()
    s2 = await client.list_tool_schemas()
    assert s1 is s2
    assert mock_arcade.tools.list.call_count == 1


async def test_list_tool_schemas_missing_tool_raises(
    mock_arcade: MagicMock,
    async_iter_page: Any,
) -> None:
    mock_arcade.tools.list = MagicMock(return_value=async_iter_page([]))
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    with pytest.raises(ConfigError):
        await client.list_tool_schemas()


def test_arcade_name_for_before_init_raises(mock_arcade: MagicMock) -> None:
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    with pytest.raises(KeyError):
        client.arcade_name_for("Gmail_SendEmail")


async def test_authorize_completed(mock_arcade: MagicMock) -> None:
    mock_arcade.tools.authorize = AsyncMock(
        return_value=AuthorizationResponse(id="auth_x", status="completed", url=None)
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    result = await client.authorize("user@x", "Gmail.SendEmail")
    assert result == AuthResult(completed=True, url=None, auth_id="auth_x")


async def test_authorize_pending(mock_arcade: MagicMock) -> None:
    mock_arcade.tools.authorize = AsyncMock(
        return_value=AuthorizationResponse(id="auth_y", status="pending", url="https://consent")
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    result = await client.authorize("user@x", "Gmail.SendEmail")
    assert result == AuthResult(completed=False, url="https://consent", auth_id="auth_y")


async def test_authorize_transport_error_wraps(mock_arcade: MagicMock) -> None:
    mock_arcade.tools.authorize = AsyncMock(side_effect=RuntimeError("boom"))
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    with pytest.raises(ArcadeToolError):
        await client.authorize("user@x", "Gmail.SendEmail")


async def test_wait_for_completion_happy(mock_arcade: MagicMock) -> None:
    mock_arcade.auth.wait_for_completion = AsyncMock(
        return_value=AuthorizationResponse(id="auth_y", status="completed", url=None)
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    await client.wait_for_completion("auth_y")


async def test_wait_for_completion_failed_status(mock_arcade: MagicMock) -> None:
    mock_arcade.auth.wait_for_completion = AsyncMock(
        return_value=AuthorizationResponse(id="auth_y", status="failed", url=None)
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    with pytest.raises(ArcadeToolError):
        await client.wait_for_completion("auth_y")


async def test_execute_success(mock_arcade: MagicMock) -> None:
    mock_arcade.tools.execute = AsyncMock(
        return_value=ExecuteToolResponse(success=True, output=Output(value={"id": "abc"}))
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    result = await client.execute("user@x", "Gmail.SendEmail", {"to": "a@b"})
    assert result == ExecuteResult(
        success=True,
        output={"id": "abc"},
        auth_url=None,
        error_message=None,
        error_kind=None,
    )


async def test_execute_auth_required_via_authorization(mock_arcade: MagicMock) -> None:
    mock_arcade.tools.execute = AsyncMock(
        return_value=ExecuteToolResponse(
            success=False,
            output=Output(
                authorization=AuthorizationResponse(
                    id="auth_z", status="pending", url="https://consent"
                ),
            ),
        )
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    result = await client.execute("user@x", "Gmail.SendEmail", {})
    assert result.success is False
    assert result.auth_url == "https://consent"
    assert result.error_kind == "AUTH_REQUIRED"


async def test_execute_auth_required_kind_falls_back_to_authorize(
    mock_arcade: MagicMock,
) -> None:
    mock_arcade.tools.execute = AsyncMock(
        return_value=ExecuteToolResponse(
            success=False,
            output=Output(
                error=OutputError(
                    can_retry=False,
                    kind="TOOL_REQUIREMENTS_NOT_MET",
                    message="needs consent",
                )
            ),
        )
    )
    mock_arcade.tools.authorize = AsyncMock(
        return_value=AuthorizationResponse(id="auth_w", status="pending", url="https://oauth")
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    result = await client.execute("user@x", "Gmail.SendEmail", {})
    assert result.success is False
    assert result.auth_url == "https://oauth"
    assert result.error_kind == "AUTH_REQUIRED"
    mock_arcade.tools.authorize.assert_awaited_once()


async def test_execute_tool_error(mock_arcade: MagicMock) -> None:
    mock_arcade.tools.execute = AsyncMock(
        return_value=ExecuteToolResponse(
            success=False,
            output=Output(
                error=OutputError(
                    can_retry=False,
                    kind="UPSTREAM_RUNTIME_BAD_REQUEST",
                    message="bad input",
                )
            ),
        )
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    result = await client.execute("user@x", "Gmail.SendEmail", {})
    assert result.success is False
    assert result.auth_url is None
    assert result.error_message == "bad input"
    assert result.error_kind == "UPSTREAM_RUNTIME_BAD_REQUEST"


async def test_execute_transport_error_raises(mock_arcade: MagicMock) -> None:
    mock_arcade.tools.execute = AsyncMock(side_effect=RuntimeError("dns"))
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    with pytest.raises(ArcadeToolError):
        await client.execute("user@x", "Gmail.SendEmail", {})


async def test_execute_403_tool_authorization_required_returns_auth_required(
    mock_arcade: MagicMock,
) -> None:
    """Arcade surfaces unmet tool auth as HTTP 403, not as success=False payload.

    Reproduces the live error
    ``arcade execute failed: Error code: 403 - {'name': 'tool_authorization_required', ...}``
    and asserts the wrapper translates it into ``AUTH_REQUIRED`` so the agent
    can drive the consent flow instead of crashing.
    """
    import httpx
    from arcadepy import PermissionDeniedError

    request = httpx.Request("POST", "https://api.arcade.dev/v1/tools/execute")
    response = httpx.Response(
        status_code=403,
        request=request,
        json={"name": "tool_authorization_required", "message": "authorization required"},
    )
    mock_arcade.tools.execute = AsyncMock(
        side_effect=PermissionDeniedError(
            "Error code: 403 - {'name': 'tool_authorization_required', "
            "'message': 'authorization required'}",
            response=response,
            body={
                "name": "tool_authorization_required",
                "message": "authorization required",
            },
        )
    )
    mock_arcade.tools.authorize = AsyncMock(
        return_value=AuthorizationResponse(id="auth_403", status="pending", url="https://oauth")
    )
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])

    result = await client.execute("user@x", "Gmail.SendEmail", {})

    assert result.success is False
    assert result.auth_url == "https://oauth"
    assert result.error_kind == "AUTH_REQUIRED"
    mock_arcade.tools.authorize.assert_awaited_once()


async def test_execute_timeout_raises(mock_arcade: MagicMock) -> None:
    mock_arcade.tools.execute = AsyncMock(side_effect=TimeoutError())
    client = ArcadeAgentClient(mock_arcade, ["Gmail.SendEmail"])
    with pytest.raises(ArcadeToolError):
        await client.execute("user@x", "Gmail.SendEmail", {})


async def test_pre_authorize_all_skips_completed(mock_arcade: MagicMock) -> None:
    statuses = {
        "Gmail.SendEmail": AuthorizationResponse(id="a1", status="completed", url=None),
        "GoogleCalendar.ListEvents": AuthorizationResponse(
            id="a2", status="pending", url="https://consent"
        ),
    }

    async def fake_authorize(*, tool_name: str, user_id: str) -> AuthorizationResponse:
        return statuses[tool_name]

    mock_arcade.tools.authorize = AsyncMock(side_effect=fake_authorize)
    client = ArcadeAgentClient(
        mock_arcade,
        ["Gmail.SendEmail", "Gmail.ListEmails", "GoogleCalendar.ListEvents"],
    )
    pending = await client.pre_authorize_all("user@x")
    assert len(pending) == 1
    assert pending[0] == AuthURLPending(
        provider_id="GoogleCalendar", auth_id="a2", url="https://consent"
    )


async def test_pre_authorize_all_empty_returns_empty(mock_arcade: MagicMock) -> None:
    client = ArcadeAgentClient(mock_arcade, [])
    assert await client.pre_authorize_all("user@x") == []
