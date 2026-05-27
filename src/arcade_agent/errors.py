"""Typed exception hierarchy for the arcade-agent package."""

from __future__ import annotations


class ArcadeAgentError(Exception):
    """Base class for every typed error raised by this package."""


class ConfigError(ArcadeAgentError):
    """Missing/invalid configuration (env var, malformed bindings file, etc.)."""


class BindingNotFound(ArcadeAgentError):
    """A binding lookup by name did not match any stored entry."""

    def __init__(self, name: str) -> None:
        super().__init__(f"binding not found: {name!r}")
        self.name = name


class BindingExists(ArcadeAgentError):
    """A binding cannot be added because the name is already taken."""

    def __init__(self, name: str) -> None:
        super().__init__(f"binding already exists: {name!r}")
        self.name = name


class ArcadeAuthRequired(ArcadeAgentError):
    """Raised (or returned as a signal) when an Arcade tool call needs consent."""

    def __init__(
        self,
        url: str,
        *,
        tool_name: str | None = None,
        auth_id: str | None = None,
    ) -> None:
        super().__init__(f"arcade authorization required: {url}")
        self.url = url
        self.tool_name = tool_name
        self.auth_id = auth_id


class ArcadeToolError(ArcadeAgentError):
    """Any non-auth Arcade execute failure (validation, upstream, timeout, ...)."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.tool_name = tool_name
        self.kind = kind


class DaemonNotRunning(ArcadeAgentError):
    """The CLI tried to talk to a daemon that is not up."""


class DaemonAlreadyRunning(ArcadeAgentError):
    """`daemon start` ran while another daemon process is alive."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"daemon already running (pid={pid})")
        self.pid = pid


class ProtocolError(ArcadeAgentError):
    """Malformed inbound/outbound JSON on the daemon socket."""
