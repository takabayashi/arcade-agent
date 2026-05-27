"""Unit tests for arcade_agent.confirm."""

from __future__ import annotations

from arcade_agent import config
from arcade_agent.confirm import is_destructive, render_summary


def test_is_destructive_table() -> None:
    for name in config.DESTRUCTIVE_TOOLS:
        assert is_destructive(name) is True
    assert is_destructive("Gmail.ListEmails") is False
    assert is_destructive("GoogleCalendar.ListEvents") is False
    assert is_destructive("does.not.exist") is False


def test_render_summary_empty_args() -> None:
    output = render_summary("Gmail.SendEmail", {})
    assert output == "Tool: Gmail.SendEmail\nArguments: (none)\n"


def test_render_summary_simple_args_sorted() -> None:
    output = render_summary("Gmail.SendEmail", {"subject": "Hi", "body": "yo", "to": "a@b"})
    lines = output.splitlines()
    assert lines[0] == "Tool: Gmail.SendEmail"
    assert lines[1] == "Arguments:"
    assert lines[2] == "  body: yo"
    assert lines[3] == "  subject: Hi"
    assert lines[4] == "  to: a@b"


def test_render_summary_truncates_long_strings() -> None:
    long_body = "x" * 700
    output = render_summary("Gmail.SendEmail", {"body": long_body})
    assert "[…+" in output
    assert "more chars]" in output
    body_line = next(line for line in output.splitlines() if line.startswith("  body:"))
    assert len(body_line) < 700


def test_render_summary_bytes_value() -> None:
    output = render_summary("Tool.X", {"blob": b"hello world"})
    assert "<11 bytes>" in output


def test_render_summary_nested_dict() -> None:
    output = render_summary("Tool.X", {"params": {"a": 1, "b": [1, 2]}})
    assert "params:" in output
    assert '"a": 1' in output


def test_render_summary_non_serializable_falls_back_to_repr() -> None:
    class Weird:
        def __repr__(self) -> str:
            return "<Weird>"

    output = render_summary("Tool.X", {"thing": Weird()})
    assert "<Weird>" in output


def test_render_summary_is_deterministic() -> None:
    args = {"z": 1, "a": 2, "m": [3, 2, 1]}
    out1 = render_summary("T", args)
    out2 = render_summary("T", args)
    assert out1 == out2
