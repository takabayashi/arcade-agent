"""Typer CLI entry point. Real subcommands land in Phase 5."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="arcade-agent",
    help="Gmail + Calendar agent powered by Arcade.dev and Anthropic Claude.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed version."""
    from arcade_agent import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
