"""The `lectern` command."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Annotated, BinaryIO

import typer
import uvicorn
from uvicorn.config import LOGGING_CONFIG

from lectern.config import DEFAULT_PORT

app = typer.Typer(help="Lectern: a local, modular AI workbench.", no_args_is_help=True)


# Where `serve` may listen. 0.0.0.0 still answers on 127.0.0.1, which the installer's health
# check, the browser opener and ocr-check's running-server check connect to; a single other
# address wouldn't, so it's not offered (a firewall keeps Lectern to one network).
LOCAL_HOST = "127.0.0.1"
EVERY_ADDRESS = "0.0.0.0"  # noqa: S104


@app.command()
def serve(
    port: int = typer.Option(DEFAULT_PORT, help="Port to listen on."),
    host: str = typer.Option(
        LOCAL_HOST,
        "--host",
        envvar="LECTERN_HOST",
        help="127.0.0.1 (this machine only), or 0.0.0.0 to be reached on your network without "
        "a proxy; that needs --public-url / LECTERN_PUBLIC_URL. Turn accounts on first "
        "(lectern user init): until then, anyone on the network who has the link gets in.",
    ),
    browser: bool = typer.Option(True, help="Open the browser once the server starts."),
    stop_on_stdin_close: bool = typer.Option(
        False,
        "--stop-on-stdin-close",
        help="Stop, as on Ctrl+C, when standard input closes: how Lectern's Windows tray app "
        "stops it (Windows has no Ctrl+C to send a process with no console).",
    ),
    public_url: Annotated[
        list[str] | None,
        typer.Option(
            "--public-url",
            help="The address people reach Lectern at: through a proxy or tunnel, or this "
            "machine's own with --host 0.0.0.0, e.g. http://lectern.lan (repeatable; default: "
            "LECTERN_PUBLIC_URL).",
        ),
    ] = None,
) -> None:
    """Start the Lectern server: on 127.0.0.1, unless LECTERN_HOST / --host says 0.0.0.0."""
    from lectern.app import create_app

    if host not in (LOCAL_HOST, EVERY_ADDRESS):
        typer.echo(
            f"LECTERN_HOST must be {LOCAL_HOST} (this machine only) or {EVERY_ADDRESS} "
            f"(every network address), not {host!r}.",
            err=True,
        )
        raise typer.Exit(2)
    public_given = public_url or os.environ.get("LECTERN_PUBLIC_URL", "").replace(",", "").strip()
    if host == EVERY_ADDRESS and not public_given:
        # Without it the guard answers 421 to everything that comes from the network. Refused
        # before the app is made: no database, no hardware probe, no token for nothing.
        typer.echo(
            f"LECTERN_HOST={EVERY_ADDRESS} needs LECTERN_PUBLIC_URL (or --public-url): the "
            f"address people will type, e.g. http://192.168.1.2:{port}.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        lectern_app = create_app(port=port, public_urls=public_url or None)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    from lectern.core import accounts

    token = lectern_app.state.token
    url = f"http://127.0.0.1:{port}/#token={token}"
    people_sign_in = accounts.accounts_on(lectern_app.state.sessions)
    for line in where_it_runs(
        port, token, lectern_app.state.public_urls, every_address=host == EVERY_ADDRESS,
        people_sign_in=people_sign_in,
    ):  # fmt: skip
        typer.echo(line)
    if browser:
        open_when_listening(url, port)
    LecternServer(
        server_config(lectern_app, port, host), stop_on_stdin_close=stop_on_stdin_close
    ).run()


def where_it_runs(
    port: int, token: str, public: list[str], *, every_address: bool, people_sign_in: bool
) -> list[str]:
    """What `serve` says as it starts: where Lectern is, and who can get in."""
    here = f"Lectern is running at http://127.0.0.1:{port}/#token={token}"
    if people_sign_in:
        # The link is this computer's; from anywhere else people sign in.
        return [here, *(f"People sign in at {origin}" for origin in public)]
    lines = []
    if every_address:
        lines.append(
            "Listening on every network address, with no login: anyone on this network who "
            "has the link can use Lectern, and on plain http the token can be read off the "
            "network."
        )
    return [*lines, here, *(f"and at {origin}/#token={token}" for origin in public)]


@app.command()
def url(
    port: int = typer.Option(DEFAULT_PORT, help="Port of the running `lectern serve`."),
) -> None:
    """Print the link to open Lectern with, its access token included: the way into a
    server with no browser (run it as the user Lectern runs as). The first LECTERN_PUBLIC_URL
    when set, else 127.0.0.1."""
    from lectern.config import get_paths
    from lectern.core.security import parse_public_urls

    token_file = get_paths().token_file
    token = token_file.read_text(encoding="utf-8").strip() if token_file.is_file() else ""
    if not token:
        typer.echo("Lectern hasn't run here yet (no access token). Start it first.", err=True)
        raise typer.Exit(1)
    try:
        public = parse_public_urls(os.environ.get("LECTERN_PUBLIC_URL", ""))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    if _accounts_on():
        # From outside the link is refused now; it opens Lectern here, as the admin.
        typer.echo(f"http://127.0.0.1:{port}/#token={token}")
        typer.echo("That link works on this computer only, and opens the admin's workspace.")
        for address in public:
            typer.echo(f"People sign in at {address.origin}")
        return
    base = public[0].origin if public else f"http://127.0.0.1:{port}"
    typer.echo(f"{base}/#token={token}")


# --- accounts ------------------------------------------------------------------------------------

user_app = typer.Typer(
    help="Accounts: who can sign in. Passwords are typed when asked, never given as arguments.",
    no_args_is_help=True,
)
app.add_typer(user_app, name="user")


def _accounts():  # noqa: ANN202
    """The app database's sessions, migrated: these commands work before Lectern has run,
    and beside one that is running."""
    from lectern.config import get_paths
    from lectern.core.db import make_engine, make_sessionmaker, migrate

    engine = make_engine(get_paths().app_db)
    migrate(engine)
    return make_sessionmaker(engine)


def _accounts_on() -> bool:
    from lectern.core import accounts

    return accounts.accounts_on(_accounts())


def _new_password() -> str:
    """Asked for twice, without showing it. Not an option: an argument would stay in the
    shell's history and show in the process list."""
    password = typer.prompt("Password", hide_input=True)
    if typer.prompt("Again", hide_input=True) != password:
        typer.echo("The two didn't match. Nothing was changed.", err=True)
        raise typer.Exit(1)
    return password


def _or_say_why(action) -> None:  # noqa: ANN001
    """Run an accounts change; what it refuses is said in its own words, without a trace."""
    from lectern.core import accounts
    from lectern.core.passwords import WeakPassword

    refusals = (
        accounts.AlreadySetUp, accounts.NotSetUp, accounts.BadName, accounts.NameTaken,
        accounts.NoSuchUser, accounts.LastAdmin, WeakPassword,
    )  # fmt: skip
    try:
        action()
    except refusals as refused:
        typer.echo(str(refused), err=True)
        raise typer.Exit(1) from None


def _accounts_or_exit():  # noqa: ANN202
    sessions = _accounts()
    from lectern.core import accounts

    if not accounts.accounts_on(sessions):
        typer.echo("Nobody signs in here yet. Start with: lectern user init <name>", err=True)
        raise typer.Exit(1)
    return sessions


@user_app.command("init")
def user_init(login: str = typer.Argument(..., help="The name you will sign in with.")) -> None:
    """Turn accounts on: this install's one user becomes the admin LOGIN, keeping its
    projects. From then on people sign in, and the link from `lectern url` opens Lectern on
    this computer only."""
    from lectern.core import accounts

    sessions = _accounts()
    if accounts.accounts_on(sessions):
        typer.echo(
            "Accounts are already set up. Change a password with: lectern user password <name>",
            err=True,
        )
        raise typer.Exit(1)
    password = _new_password()
    made: list = []
    _or_say_why(lambda: made.append(accounts.init_admin(sessions, login, password)))
    typer.echo(
        f"Accounts are on. {made[0].login} is the admin. Add people with: lectern user add <name>"
    )


@user_app.command("add")
def user_add(
    login: str = typer.Argument(..., help="The name they will sign in with."),
    admin: bool = typer.Option(
        False, "--admin", help="May also manage models, settings and accounts."
    ),
) -> None:
    """Add a person. They get a workspace of their own."""
    from lectern.core import accounts

    sessions = _accounts_or_exit()
    password = _new_password()
    made: list = []
    _or_say_why(lambda: made.append(accounts.add_user(sessions, login, password, admin=admin)))
    typer.echo(f"Added {made[0].login} ({made[0].role}).")


@user_app.command("password")
def user_password(login: str = typer.Argument(..., help="Whose password to set.")) -> None:
    """Set a new password, for someone who forgot theirs. It signs them out everywhere."""
    from lectern.core import accounts

    sessions = _accounts_or_exit()
    _or_say_why(lambda: accounts.describe(sessions, login) or _no_such(login))
    password = _new_password()
    _or_say_why(lambda: accounts.set_password(sessions, login, password))
    typer.echo(f"{login.strip().lower()} has a new password, and is signed out everywhere.")


def _no_such(login: str) -> None:
    from lectern.core import accounts

    raise accounts.NoSuchUser(f"No account is named {login!r}.")


@user_app.command("list")
def user_list() -> None:
    """Who has an account: name, role, and whether it is disabled."""
    from lectern.core import accounts

    sessions = _accounts()
    if not accounts.accounts_on(sessions):
        typer.echo("Nobody signs in here yet. Start with: lectern user init <name>")
        return
    for person in accounts.describe(sessions):
        off = "  disabled" if person["disabled"] else ""
        typer.echo(f"{person['login']}  {person['role']}{off}")


@user_app.command("disable")
def user_disable(login: str = typer.Argument(..., help="Whose account to disable.")) -> None:
    """Stop someone signing in, and sign them out. Their projects stay."""
    from lectern.core import accounts

    sessions = _accounts_or_exit()
    _or_say_why(lambda: accounts.set_disabled(sessions, login, True))
    typer.echo(f"{login.strip().lower()} can't sign in any more.")


@user_app.command("enable")
def user_enable(login: str = typer.Argument(..., help="Whose account to enable again.")) -> None:
    """Let a disabled account sign in again."""
    from lectern.core import accounts

    sessions = _accounts_or_exit()
    _or_say_why(lambda: accounts.set_disabled(sessions, login, False))
    typer.echo(f"{login.strip().lower()} can sign in again.")


# The page keeps event streams open (runtime and download progress). Uvicorn's graceful
# shutdown waits for every connection to close before it runs the app's shutdown hook, so an
# open browser tab would keep `lectern serve` alive forever after Ctrl+C. After this many
# seconds it drops them, then the shutdown hook stops the model runners.
SHUTDOWN_GRACE_SECONDS = 3


class LecternServer(uvicorn.Server):
    """Tells the app it's shutting down the moment Ctrl+C arrives, before uvicorn starts
    waiting on connections, so the event streams can end by themselves.

    `stop_on_stdin_close`: standard input closing is a Ctrl+C too. What's written to it is
    ignored."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        stop_on_stdin_close: bool = False,
        stdin: BinaryIO | None = None,
    ) -> None:
        super().__init__(config)
        self._stop_on_stdin_close = stop_on_stdin_close
        self._stdin = stdin

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        if self._stop_on_stdin_close:
            loop = asyncio.get_running_loop()
            stdin = self._stdin or sys.stdin.buffer
            threading.Thread(
                target=self._stop_when_closed, args=(loop, stdin), name="stdin", daemon=True
            ).start()
        await super().serve(sockets)

    def _stop_when_closed(self, loop: asyncio.AbstractEventLoop, stdin: BinaryIO) -> None:
        with contextlib.suppress(OSError, ValueError):
            while stdin.read(4096):
                pass
        with contextlib.suppress(RuntimeError):  # the loop has closed: it's stopping already
            loop.call_soon_threadsafe(self._stop)

    def _stop(self) -> None:
        """On the loop: stop as Ctrl+C does (handle_exit), but as a request, not a signal, so
        uvicorn doesn't raise one again once it's down and the exit status stays 0."""
        self.config.app.state.shutting_down.set()
        self.should_exit = True

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


def server_config(lectern_app, port: int, host: str = LOCAL_HOST) -> uvicorn.Config:  # noqa: ANN001
    return uvicorn.Config(
        lectern_app,
        host=host,
        port=port,
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
        log_config=_log_config(),
    )


def _log_config() -> dict:
    """uvicorn's own, with the time at the start of each line: without it a slow request or
    a restart can't be placed in the log (the hang with three tabs open couldn't be)."""
    config = copy.deepcopy(LOGGING_CONFIG)
    for formatter in config["formatters"].values():
        formatter["fmt"] = "%(asctime)s " + formatter["fmt"]
        formatter["datefmt"] = "%Y-%m-%d %H:%M:%S"
    return config


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
    prompt_set: str = typer.Option("calc", "--set", help="Prompt set: calc, tools, long or web."),
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
        if started.status_code in (404, 405):  # the server wasn't started with the dev tools
            typer.echo(
                "The running server has no developer tools. Restart it with LECTERN_DEV=1 "
                "(e.g. LECTERN_DEV=1 uv run lectern serve) and run this again.",
                err=True,
            )
            raise typer.Exit(1)
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
