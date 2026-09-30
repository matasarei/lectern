"""The `lectern` command."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading
import time
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from lectern.config import DEFAULT_PORT

app = typer.Typer(help="Lectern: a local, modular AI workbench.", no_args_is_help=True)


@app.command()
def serve(
    port: int = typer.Option(DEFAULT_PORT, help="Port on 127.0.0.1."),
    browser: bool = typer.Option(True, help="Open the browser once the server starts."),
) -> None:
    """Start the Lectern server on 127.0.0.1 (never on any other address)."""
    from lectern.app import create_app

    lectern_app = create_app(port=port)
    url = f"http://127.0.0.1:{port}/#token={lectern_app.state.token}"
    typer.echo(f"Lectern is running at {url}")
    if browser:
        open_when_listening(url, port)
    LecternServer(server_config(lectern_app, port)).run()


# The page keeps event streams open (runtime and download progress). Uvicorn's graceful
# shutdown waits for every connection to close before it runs the app's shutdown hook, so an
# open browser tab would keep `lectern serve` alive forever after Ctrl+C. After this many
# seconds it drops them, then the shutdown hook stops the model runners.
SHUTDOWN_GRACE_SECONDS = 3


class LecternServer(uvicorn.Server):
    """Tells the app it's shutting down the moment Ctrl+C arrives, before uvicorn starts
    waiting on connections, so the event streams can end by themselves."""

    def handle_exit(self, sig, frame) -> None:  # noqa: ANN001
        # uvicorn installs this with signal.signal, so it runs as a plain signal handler, not
        # as an event-loop callback. Hand the flag to the loop through its thread-safe entry
        # point, which also wakes it at once (on Windows' event loop too).
        stopping = self.config.app.state.shutting_down
        try:
            asyncio.get_running_loop().call_soon_threadsafe(stopping.set)
        except RuntimeError:  # no loop running yet or any more
            stopping.set()
        super().handle_exit(sig, frame)


def server_config(lectern_app, port: int) -> uvicorn.Config:  # noqa: ANN001
    return uvicorn.Config(
        lectern_app,
        host="127.0.0.1",
        port=port,
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )


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
    # 10 s, not 5: llama.cpp alone spends ~4.7 s loading the 9B model and setting up its context
    # on an 8 GB card (M0 acceptance on Windows, docs/acceptance/m0-windows-8gb-results.md).
    "recommended": (30.0, 10.0),
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


@dev_app.command("agent-check")
def agent_check(
    prompt_set: str = typer.Option("calc", "--set", help="Prompt set: calc, tools or long."),
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Run only these prompt ids.")
    ] = None,
    allow_cards: bool = typer.Option(
        True, help="Answer cards (downloads, the sandbox build) with Allow."
    ),
    port: int = typer.Option(DEFAULT_PORT, help="Port of the running `lectern serve`."),
) -> None:
    """Send a prompt set to a running server, one fresh chat each, and score the answers."""
    from lectern.dev.agent_check import run_set, summary

    with _server(port) as client:
        try:
            outcomes = run_set(client, prompt_set, allow_cards=allow_cards, only=only or None,
                               echo=typer.echo)  # fmt: skip
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
    typer.echo(summary(outcomes))
    if not all(o.used_tools for o in outcomes):
        raise typer.Exit(1)


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


@dev_app.command("ocr-check")
def ocr_check(
    folder: Annotated[Path, typer.Argument(help="A folder with your scans (PDFs and images).")],
    model: Annotated[
        list[str] | None, typer.Option("--model", help="Score only these catalog models.")
    ] = None,
    draft: str | None = typer.Option(
        None, help="Write missing reference/<page>.txt from this model's text, for you to correct."
    ),
    pages: str | None = typer.Option(None, help="Only these pages, e.g. 1-5,8."),
    allow_server: bool = typer.Option(
        False, help="Run while `lectern serve` is running (it shares the GPU; timings suffer)."
    ),
    port: int = typer.Option(DEFAULT_PORT, help="The port a running `lectern serve` would use."),
) -> None:
    """Score OCR models on your own scans: error rates against your corrected text, with
    time and memory. Writes only under <folder>/.ocr-check (and reference/ with --draft)."""
    from lectern.dev import ocr_check as check
    from lectern.modules.ocr.pages import NotAPageSource

    folder = folder.expanduser()
    if not folder.is_dir():
        raise typer.BadParameter(f"{folder} isn't a folder.")
    running = sorted({*_serving_ports(), *([port] if _listening(port) else [])})
    if not allow_server and running:
        ports = ", ".join(str(p) for p in running)
        typer.echo(
            f"A Lectern server is running (port {ports}). It shares the GPU, so the timings "
            "and memory would be off. Stop it, or pass --allow-server.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        models = check.candidates(model)
        if draft and draft not in [entry.id for _, entry in models]:
            models += check.candidates([draft])
        tags = check.load_tags(folder)  # before the models run, so a typo fails fast
        chosen = check.pick_pages(check.prepare(folder, echo=typer.echo), pages)
    except (ValueError, NotAPageSource) as exc:
        raise typer.BadParameter(str(exc)) from None
    runtime = _local_runtime()
    runs = asyncio.run(_run_ocr_check(runtime, folder, chosen, models))
    text = check.report(folder, runs, chosen, tags, runtime.swaps)
    saved = folder / check.WORK_DIR / f"report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    saved.write_text(text, encoding="utf-8")
    typer.echo("\n" + text + f"\nSaved to {saved}")
    if draft:
        drafter = next(run for run in runs if run.model_id == draft)
        if drafter.skipped:
            typer.echo(f"Nothing drafted: {drafter.title} was skipped ({drafter.skipped}).")
        else:
            written = check.draft_references(folder, drafter, chosen)
            typer.echo(
                f"Drafted {written} references in {folder / check.REFERENCE_DIR}. Correct them "
                "by hand, then run again without --draft."
            )
    if all(run.skipped for run in runs):
        raise typer.Exit(1)


async def _run_ocr_check(runtime, folder: Path, pages, models):  # noqa: ANN001, ANN202
    from lectern.dev.ocr_check import run_models

    try:
        return await run_models(runtime, folder, pages, models, echo=typer.echo)
    finally:
        await runtime.stop_all()  # also on Ctrl+C: no llama-server is left running


def _local_runtime():  # noqa: ANN202
    """The same Model Runtime `lectern serve` builds, in this process."""
    from lectern.config import get_paths
    from lectern.core.db import make_engine, make_sessionmaker, migrate
    from lectern.downloads.manager import DownloadManager
    from lectern.hardware.probe import probe
    from lectern.runtime.wiring import build_runtime

    paths = get_paths()
    engine = make_engine(paths.app_db)
    migrate(engine)
    sessions = make_sessionmaker(engine)
    return build_runtime(paths, DownloadManager(paths.models, sessions), probe(), sessions)


def _serving_ports() -> list[int]:
    """Ports of every `lectern serve` running on this machine, whatever its port or Lectern
    home: any of them shares the GPU. Nothing records a server's port, so its command line
    is read."""
    import psutil

    ports = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        args = process.info["cmdline"] or []
        names = [a.replace("\\", "/").rsplit("/", 1)[-1] for a in args]  # Windows paths too
        if process.info["pid"] == os.getpid() or "serve" not in args:
            continue
        if not any(name.startswith("lectern") for name in names[:3]):  # lectern, lectern.exe
            continue
        after = args[args.index("--port") + 1] if "--port" in args[:-1] else None
        ports.append(int(after) if after and after.isdigit() else DEFAULT_PORT)
    return ports


def _listening(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
        return True
    return False
