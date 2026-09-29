"""The `lectern` command."""

from __future__ import annotations

import json

import typer

from lectern.config import DEFAULT_PORT

app = typer.Typer(help="Lectern: a local, modular AI workbench.", no_args_is_help=True)


@app.command()
def serve(port: int = typer.Option(DEFAULT_PORT, help="Port on 127.0.0.1.")) -> None:
    """Start the Lectern server on 127.0.0.1."""
    import uvicorn

    from lectern.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=port)


@app.command()
def doctor() -> None:
    """Print what Lectern knows about this machine, as JSON."""
    typer.echo(json.dumps({"status": "not implemented yet"}, indent=2))
