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


# --- talking to a running server ------------------------------------------------------------

TARGETS = {  # tier -> (minimum tokens/s, maximum seconds to the first token after a load)
    "minimum": (10.0, 8.0),
    "recommended": (30.0, 5.0),
    "comfortable": (30.0, 5.0),
}
BENCH_PROMPTS = (
    "Explain in three sentences what a lectern is.",
    "List five things a local OCR tool is useful for, one line each.",
    "Write a four-line poem about memory.",
)


def _server(port: int):  # noqa: ANN202
    import httpx2

    from lectern.config import get_paths
    from lectern.core.security import load_or_create_token

    token = load_or_create_token(get_paths().token_file)
    base = f"http://127.0.0.1:{port}"
    return httpx2.Client(
        base_url=base,
        headers={"X-Lectern-Token": token, "Origin": base},
        timeout=httpx2.Timeout(600.0, connect=5.0),
    )


def _stream_chat(client, prompt: str, *, echo: bool, max_tokens: int = 512) -> dict:  # noqa: ANN001
    body = {"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    done: dict = {}
    with client.stream("POST", "/api/chat", json=body) as response:
        if response.status_code != 200:
            raise typer.BadParameter(f"The server answered {response.status_code}.")
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line.removeprefix("data: "))
            if event["type"] == "delta" and echo:
                typer.echo(event["text"], nl=False)
            elif event["type"] == "error":
                typer.echo(f"\nError: {event['detail']}", err=True)
                raise typer.Exit(1)
            elif event["type"] == "done":
                done = event
    if echo:
        typer.echo()
    if not done:
        typer.echo("Error: the server ended the reply before it finished.", err=True)
        raise typer.Exit(1)
    return done


@app.command()
def chat(
    prompt: str,
    port: int = typer.Option(DEFAULT_PORT, help="Port of the running `lectern serve`."),
) -> None:
    """Send one message to the main model of a running server; print the reply and its speed."""
    with _server(port) as client:
        done = _stream_chat(client, prompt, echo=True)
    typer.echo(
        f"[first token {done.get('ttft')} s · {done.get('tps')} tokens/s · "
        f"{done.get('tokens')} tokens]"
    )


@app.command()
def bench(
    port: int = typer.Option(DEFAULT_PORT, help="Port of the running `lectern serve`."),
) -> None:
    """Measure chat against this tier's targets: first reply after a load, then speed."""
    with _server(port) as client:
        tier = client.get("/api/runtime").json()["tier"]
        client.post("/api/runtime/unload", json={})
        cold = _stream_chat(client, BENCH_PROMPTS[0], echo=False, max_tokens=128)
        warm = [_stream_chat(client, p, echo=False, max_tokens=200) for p in BENCH_PROMPTS]
        state = client.get("/api/runtime").json()
    speeds = sorted(w["tps"] for w in warm)
    median = speeds[len(speeds) // 2]
    min_tps, max_ttft = TARGETS.get(tier, (None, None))
    rows = [
        ("tier", tier, ""),
        ("first token after a load", f"{cold['ttft']} s", f"≤ {max_ttft} s" if max_ttft else "—"),
        ("chat speed (median of 3)", f"{median} tokens/s", f"≥ {min_tps}" if min_tps else "—"),
        ("model", ", ".join(r["model_id"] for r in state["residents"]), ""),
    ]
    for name, value, target in rows:
        typer.echo(f"{name:<28} {value:<22} {target}")
    passed = (min_tps is None or median >= min_tps) and (
        max_ttft is None or (cold["ttft"] or 1e9) <= max_ttft
    )
    typer.echo("PASS" if passed else "BELOW TARGET")
    if not passed:
        raise typer.Exit(1)


dev_app = typer.Typer(help="Developer tools.", no_args_is_help=True)
app.add_typer(dev_app, name="dev")


@dev_app.command("swap-test")
def swap_test(
    pages: int = typer.Option(12, help="Pages per phase."),
    budget_mb: int | None = typer.Option(None, help="Memory budget for the test, in MiB."),
    fake_model: bool = typer.Option(False, help="Use the chat model as a stand-in OCR model."),
    port: int = typer.Option(DEFAULT_PORT, help="Port of the running `lectern serve`."),
) -> None:
    """Run a fake OCR job (every page through ocr, then through main) and count the swaps."""
    body = {"pages": pages, "budget_mb": budget_mb, "fake_model": fake_model}
    with _server(port) as client:
        started = client.post("/api/dev/swap-test", json=body)
        if started.status_code != 202:
            typer.echo(started.json().get("detail", started.text), err=True)
            raise typer.Exit(1)
        while True:
            result = client.get("/api/dev/swap-test").json()
            if result["finished"]:
                break
            time.sleep(0.5)
    if result["error"]:
        typer.echo(f"error: {result['error']}", err=True)
        raise typer.Exit(1)
    typer.echo(f"pages: {result['done']}/{pages * 2}")
    typer.echo(f"swaps: {result['swaps']}")
