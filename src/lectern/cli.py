"""The `lectern` command."""

from __future__ import annotations

import json
import webbrowser

import typer

from lectern.config import DEFAULT_PORT

app = typer.Typer(help="Lectern: a local, modular AI workbench.", no_args_is_help=True)


@app.command()
def serve(
    port: int = typer.Option(DEFAULT_PORT, help="Port on 127.0.0.1."),
    browser: bool = typer.Option(True, help="Open the browser once the server starts."),
) -> None:
    """Start the Lectern server on 127.0.0.1 (never on any other address)."""
    import uvicorn

    from lectern.app import create_app

    lectern_app = create_app(port=port)
    url = f"http://127.0.0.1:{port}/#token={lectern_app.state.token}"
    typer.echo(f"Lectern is running at {url}")
    if browser:
        webbrowser.open(url)
    uvicorn.run(lectern_app, host="127.0.0.1", port=port)


@app.command()
def doctor() -> None:
    """Print what Lectern knows about this machine, as JSON."""
    from lectern.api.system import system_report
    from lectern.config import get_paths

    typer.echo(json.dumps(system_report(get_paths()), indent=2))
