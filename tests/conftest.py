"""Shared pytest fixtures for arcade-agent tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

from arcade_agent import config as _config  # noqa: E402
from arcade_agent.bindings import Binding, BindingStore  # noqa: E402


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


class AsyncIterPage:
    """Async iterator usable in place of arcadepy's AsyncPaginator.

    `arcadepy.tools.list` returns an `AsyncPaginator` that can be iterated with
    `async for`; this stand-in mimics just that contract.
    """

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self._iter: Any = None

    def __aiter__(self) -> AsyncIterPage:
        self._iter = iter(self._items)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


@pytest.fixture
def async_iter_page() -> type[AsyncIterPage]:
    """Expose the async iterator helper class as a fixture."""
    return AsyncIterPage


def build_tool_definition(
    *,
    qualified_name: str,
    description: str = "test tool",
    parameters: list[dict[str, Any]] | None = None,
    formatted_schema: dict[str, Any] | None = None,
) -> Any:
    """Build a real ToolDefinition pydantic model (so attribute access matches prod)."""
    from arcadepy.types.tool_definition import (
        Input,
        InputParameter,
        Requirements,
        RequirementsAuthorization,
        ToolDefinition,
        Toolkit,
    )
    from arcadepy.types.value_schema import ValueSchema

    toolkit_name, _, tool_name = qualified_name.partition(".")
    params: list[InputParameter] = []
    for p in parameters or []:
        params.append(
            InputParameter(
                name=p["name"],
                value_schema=ValueSchema(val_type=p.get("type", "string")),
                description=p.get("description"),
                required=p.get("required", False),
            )
        )
    return ToolDefinition(
        fully_qualified_name=f"{qualified_name}@1.0.0",
        qualified_name=qualified_name,
        name=tool_name,
        toolkit=Toolkit(name=toolkit_name, description=None, version="1.0.0"),
        description=description,
        input=Input(parameters=params),
        formatted_schema=formatted_schema,
        requirements=Requirements(
            authorization=RequirementsAuthorization(provider_id="google", provider_type="oauth2"),
        ),
    )


@pytest.fixture
def make_tool_definition() -> Any:
    """Expose the ToolDefinition builder as a fixture."""
    return build_tool_definition


@pytest.fixture
def mock_arcade() -> MagicMock:
    """A MagicMock shaped like arcadepy.AsyncArcade with happy-path defaults wired."""
    from arcadepy import AsyncArcade
    from arcadepy.types.execute_tool_response import ExecuteToolResponse, Output
    from arcadepy.types.shared.authorization_response import AuthorizationResponse

    mock = MagicMock(spec=AsyncArcade)

    tools_ns = MagicMock()

    def _list(**kwargs: Any) -> AsyncIterPage:
        return AsyncIterPage([])

    tools_ns.list = MagicMock(side_effect=_list)
    tools_ns.authorize = AsyncMock(
        return_value=AuthorizationResponse(id="auth_1", status="completed", url=None)
    )
    tools_ns.execute = AsyncMock(
        return_value=ExecuteToolResponse(success=True, output=Output(value={"ok": True}))
    )
    mock.tools = tools_ns

    auth_ns = MagicMock()
    auth_ns.wait_for_completion = AsyncMock(
        return_value=AuthorizationResponse(id="auth_1", status="completed", url=None)
    )
    mock.auth = auth_ns

    return mock


@pytest.fixture
def mock_anthropic() -> MagicMock:
    """MagicMock shaped like anthropic.AsyncAnthropic with helpers attached."""
    import anthropic
    from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

    mock = MagicMock(spec=anthropic.AsyncAnthropic)
    mock.messages = MagicMock()
    mock.messages.create = AsyncMock()

    def make_message(*blocks: Any, stop_reason: str = "end_turn") -> Message:
        return Message(
            id="msg_test",
            role="assistant",
            content=list(blocks),
            model="claude-sonnet-4-5",
            stop_reason=stop_reason,
            stop_sequence=None,
            type="message",
            usage=Usage(input_tokens=10, output_tokens=10),
        )

    def text_block(t: str) -> TextBlock:
        return TextBlock(type="text", text=t, citations=None)

    def tool_use_block(name: str, args: dict[str, Any], id: str = "tu_1") -> ToolUseBlock:
        return ToolUseBlock(type="tool_use", id=id, name=name, input=args)

    helpers = type(
        "Helpers",
        (),
        {
            "make_message": staticmethod(make_message),
            "text": staticmethod(text_block),
            "tool_use": staticmethod(tool_use_block),
        },
    )
    mock.helpers = helpers
    return mock
