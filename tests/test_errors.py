"""Tests for the typed exception hierarchy in arcade_agent.errors."""

from __future__ import annotations

import pytest

from arcade_agent.errors import (
    ArcadeAgentError,
    ArcadeAuthRequired,
    ArcadeToolError,
    BindingExists,
    BindingNotFound,
    ConfigError,
    DaemonAlreadyRunning,
    DaemonNotRunning,
    ProtocolError,
)


def test_all_subclass_arcade_agent_error() -> None:
    for exc_cls in (
        ConfigError,
        BindingNotFound,
        BindingExists,
        ArcadeAuthRequired,
        ArcadeToolError,
        DaemonNotRunning,
        DaemonAlreadyRunning,
        ProtocolError,
    ):
        assert issubclass(exc_cls, ArcadeAgentError)
        assert issubclass(exc_cls, Exception)


def test_binding_not_found_carries_name() -> None:
    exc = BindingNotFound("personal")
    assert exc.name == "personal"
    assert "personal" in str(exc)


def test_binding_exists_carries_name() -> None:
    exc = BindingExists("personal")
    assert exc.name == "personal"
    assert "personal" in str(exc)


def test_arcade_auth_required_carries_fields() -> None:
    exc = ArcadeAuthRequired("https://x", tool_name="Gmail.SendEmail", auth_id="auth_1")
    assert exc.url == "https://x"
    assert exc.tool_name == "Gmail.SendEmail"
    assert exc.auth_id == "auth_1"
    assert "https://x" in str(exc)


def test_arcade_tool_error_carries_fields() -> None:
    exc = ArcadeToolError("boom", tool_name="Gmail.SendEmail", kind="UPSTREAM_RUNTIME_SERVER_ERROR")
    assert exc.message == "boom"
    assert exc.tool_name == "Gmail.SendEmail"
    assert exc.kind == "UPSTREAM_RUNTIME_SERVER_ERROR"
    assert str(exc) == "boom"


def test_daemon_already_running_carries_pid() -> None:
    exc = DaemonAlreadyRunning(4242)
    assert exc.pid == 4242
    assert "4242" in str(exc)


def test_can_raise_and_catch_via_base() -> None:
    with pytest.raises(ArcadeAgentError):
        raise ProtocolError("bad json")
