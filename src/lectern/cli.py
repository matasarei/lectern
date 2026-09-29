"""The `lectern` command."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
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
        open_when_listening(url, port)
    uvicorn.run(lectern_app, host="127.0.0.1", port=port)


def open_when_listening(url: str, port: int, *, wait_seconds: float = 30.0) -> threading.Thread:
    """Open the browser once the server accepts connections, not before: a browser that
    arrives first shows a connection error."""

    def wait_then_open() -> None:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
                webbrowser.open(url)
                return
            time.sleep(0.1)

    thread = threading.Thread(target=wait_then_open, daemon=True)
    thread.start()
    return thread


@app.command()
def doctor(
    runners: bool = typer.Option(False, "--runners", help="Also show the llama.cpp build."),
) -> None:
    """Print what Lectern knows about this machine, as JSON."""
    from lectern.api.system import system_report
    from lectern.config import get_paths

    paths = get_paths()
    report = system_report(paths)
    if runners:
        from lectern.hardware.probe import probe
        from lectern.runners.binaries import (
            LLAMA_CPP_BUILD,
            LlamaCppInstaller,
            UnsupportedPlatform,
            select_variant,
        )

        try:
            variant = select_variant(probe())
            server = LlamaCppInstaller(paths.runners).server_path(variant)
            report["runners"] = {
                "llama_cpp_build": LLAMA_CPP_BUILD,
                "variant": variant,
                "installed": server is not None,
                "server": str(server) if server else None,
            }
        except UnsupportedPlatform as exc:
            report["runners"] = {"error": str(exc)}
    typer.echo(json.dumps(report, indent=2))
