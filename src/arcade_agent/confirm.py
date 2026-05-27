"""Pure helpers for the destructive-tool confirmation gate.

No IO, no async, fully deterministic. The CLI uses these to render a confirmation
prompt and the agent uses :func:`is_destructive` to decide whether a confirmation
must be requested before running a tool.
"""

from __future__ import annotations

import json
from typing import Any

from arcade_agent import config

_TRUNCATE_AT = 500
_TRUNCATE_SUFFIX = " […+{} more chars]"


def is_destructive(tool_name: str) -> bool:
    """True iff the *Arcade* tool_name (dotted form) is in :data:`config.DESTRUCTIVE_TOOLS`."""
    return tool_name in config.DESTRUCTIVE_TOOLS


def _truncate(text: str) -> str:
    if len(text) <= _TRUNCATE_AT:
        return text
    extra = len(text) - _TRUNCATE_AT
    return text[:_TRUNCATE_AT] + _TRUNCATE_SUFFIX.format(extra)


def _format_value(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, (list, dict, tuple, set)):
        try:
            rendered = json.dumps(value, indent=2, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            rendered = repr(value)
        return _truncate(rendered)
    try:
        rendered = repr(value)
    except Exception:  # pragma: no cover - extreme defensiveness
        rendered = "<unrepresentable>"
    return _truncate(rendered)


def render_summary(tool_name: str, args: dict[str, Any]) -> str:
    """Multi-line human-readable summary for the confirmation prompt.

    Layout::

        Tool: <tool_name>
        Arguments:
          <key>: <value>
          ...

    Empty `args` produces ``Arguments: (none)``. Keys are sorted alphabetically for
    deterministic output and values are truncated per the rules in the module docstring.
    """
    lines = [f"Tool: {tool_name}"]
    if not args:
        lines.append("Arguments: (none)")
        return "\n".join(lines) + "\n"

    lines.append("Arguments:")
    for key in sorted(args):
        rendered = _format_value(args[key])
        if "\n" in rendered:
            indented = rendered.replace("\n", "\n    ")
            lines.append(f"  {key}: {indented}")
        else:
            lines.append(f"  {key}: {rendered}")
    return "\n".join(lines) + "\n"
