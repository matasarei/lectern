from __future__ import annotations

import socket

import pytest

from lectern import cli


def test_browser_waits_until_the_port_listens(monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        thread = cli.open_when_listening("http://x/#token=t", port, wait_seconds=5)
        thread.join(0.5)
        assert opened == []  # bound but not listening yet: nothing opened
        listener.listen()
        thread.join(5)
    assert opened == ["http://x/#token=t"]


def test_browser_is_not_opened_if_the_server_never_starts(monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cli.open_when_listening("http://x/", port, wait_seconds=0.5).join(2)
    assert opened == []


# --- lectern dev ocr-check -----------------------------------------------------------------


SERVING_PORTS = cli._serving_ports  # the real one, before the fixture below replaces it


@pytest.fixture(autouse=True)
def no_servers_running(monkeypatch):
    """A `lectern serve` you happen to be running mustn't change what these tests see."""
    monkeypatch.setattr(cli, "_serving_ports", lambda: [])


def test_a_running_server_is_found_on_any_port(monkeypatch):
    from types import SimpleNamespace

    python = "/usr/bin/python3"
    processes = [
        {"pid": 1, "cmdline": [python, "/venv/bin/lectern", "serve", "--port", "8799"]},
        {"pid": 2, "cmdline": ["C:\\venv\\Scripts\\lectern.exe", "serve"]},
        {"pid": 3, "cmdline": ["uv", "run", "lectern", "dev", "ocr-check", "scans"]},
        {"pid": 4, "cmdline": [python, "-m", "http.server", "serve"]},
        {"pid": 5, "cmdline": None},  # a process we may not read
    ]
    fake = [SimpleNamespace(info=p) for p in processes]
    monkeypatch.setattr("psutil.process_iter", lambda attrs: fake)

    assert SERVING_PORTS() == [8799, 8765]


def _ocr_check(*args: str):  # noqa: ANN202
    from typer.testing import CliRunner

    return CliRunner().invoke(cli.app, ["dev", "ocr-check", *args])


def test_ocr_check_needs_a_folder(tmp_path):
    result = _ocr_check(str(tmp_path / "nope"))
    assert result.exit_code == 2 and "isn't a folder" in result.output


def test_ocr_check_wont_share_the_gpu_with_a_running_server(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = _ocr_check(str(tmp_path), "--port", str(port))
    assert result.exit_code == 1 and "pass --allow-server" in result.output


def test_ocr_check_scores_drafts_and_stops_the_models(tmp_path, monkeypatch, free_port):
    from PIL import Image

    from .test_ocr_check import PADDLE, FakeRuntime

    folder = tmp_path / "scans"
    folder.mkdir()
    Image.new("L", (300, 400), 255).save(folder / "page.png")
    runtime = FakeRuntime({PADDLE: ["Договір оренди"]})
    monkeypatch.setattr(cli, "_local_runtime", lambda: runtime)

    result = _ocr_check(
        str(folder), "--model", "paddleocr-vl-1.6", "--draft", "paddleocr-vl-1.6",
        "--port", str(free_port),
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "| PaddleOCR-VL 1.6 | 1 | – | – |" in result.output  # no reference until the draft
    assert "Drafted 1 references" in result.output
    assert (folder / "reference" / "page.txt").read_text(encoding="utf-8") == "Договір оренди\n"
    assert runtime.stopped and list((folder / ".ocr-check").glob("report-*.md"))


def test_ocr_check_says_why_nothing_was_drafted(tmp_path, monkeypatch, free_port):
    from PIL import Image

    from .test_ocr_check import FakeRuntime

    folder = tmp_path / "scans"
    folder.mkdir()
    Image.new("L", (300, 400), 255).save(folder / "page.png")
    monkeypatch.setattr(cli, "_local_runtime", lambda: FakeRuntime({}))

    result = _ocr_check(str(folder), "--model", "paddleocr-vl-1.6", "--draft", "paddleocr-vl-1.6",
                        "--port", str(free_port))  # fmt: skip

    assert (
        "Nothing drafted: PaddleOCR-VL 1.6 was skipped (paddleocr-vl-1.6 isn't downloaded"
        in result.output
    )
    assert not (folder / "reference").exists()


def test_ocr_check_stops_on_a_bad_tags_file_before_any_model_runs(tmp_path, monkeypatch, free_port):
    folder = tmp_path / "scans"
    folder.mkdir()
    (folder / "tags.yaml").write_text("- not a mapping")
    monkeypatch.setattr(cli, "_local_runtime", lambda: pytest.fail("no model should load"))

    result = _ocr_check(str(folder), "--port", str(free_port))

    assert result.exit_code == 2 and "tags.yaml should have one line per page" in result.output


def _run(*args: str):  # noqa: ANN202
    from typer.testing import CliRunner

    return CliRunner().invoke(cli.app, list(args))


def test_url_prints_the_link_with_the_token_from_the_running_install(paths, monkeypatch):
    from lectern.core.security import load_or_create_token

    token = load_or_create_token(paths.token_file)
    monkeypatch.delenv("LECTERN_PUBLIC_URL", raising=False)
    assert _run("url").output.strip() == f"http://127.0.0.1:8765/#token={token}"
    assert _run("url", "--port", "9000").output.strip() == f"http://127.0.0.1:9000/#token={token}"
    monkeypatch.setenv("LECTERN_PUBLIC_URL", "http://lectern.lan/, https://other.lan")
    assert _run("url").output.strip() == f"http://lectern.lan/#token={token}"


def test_url_says_so_when_lectern_has_never_run(paths):
    result = _run("url")
    assert result.exit_code == 1 and "hasn't run here yet" in result.output


def test_serve_refuses_a_malformed_public_url_before_starting(paths, monkeypatch):
    started = []
    monkeypatch.setattr(cli.LecternServer, "run", lambda self: started.append(True))
    result = _run("serve", "--no-browser", "--public-url", "lectern.lan")
    assert result.exit_code == 2 and "LECTERN_PUBLIC_URL must be like" in result.output
    assert started == []


def test_serve_prints_the_public_link_too(paths, monkeypatch):
    monkeypatch.setattr(cli.LecternServer, "run", lambda self: None)
    result = _run("serve", "--no-browser", "--public-url", "http://lectern.lan")
    assert "Lectern is running at http://127.0.0.1:8765/#token=" in result.output
    assert "and at http://lectern.lan/#token=" in result.output


def _serve_host(monkeypatch, *args: str) -> tuple[object, list[str]]:
    """Run `serve` without listening: the result, and the host uvicorn was given."""
    hosts: list[str] = []
    monkeypatch.setattr(cli.LecternServer, "run", lambda self: hosts.append(self.config.host))
    return _run("serve", "--no-browser", *args), hosts


@pytest.mark.parametrize("unset", [True, False])
def test_serve_listens_on_127_0_0_1_by_default(paths, monkeypatch, unset):
    if unset:
        monkeypatch.delenv("LECTERN_HOST", raising=False)
    else:
        monkeypatch.setenv("LECTERN_HOST", "")  # LECTERN_HOST= in lectern.env: the default
    result, hosts = _serve_host(monkeypatch)
    assert result.exit_code == 0, result.output
    assert hosts == ["127.0.0.1"] and "every network address" not in result.output


def test_lectern_host_0_0_0_0_listens_everywhere_and_warns(paths, monkeypatch):
    monkeypatch.setenv("LECTERN_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setenv("LECTERN_PUBLIC_URL", "http://192.168.1.2:8765")
    result, hosts = _serve_host(monkeypatch)
    assert result.exit_code == 0, result.output
    assert hosts == ["0.0.0.0"]  # noqa: S104
    assert "every network address, with no login" in result.output
    assert "and at http://192.168.1.2:8765/#token=" in result.output


def test_serve_host_option_does_the_same(paths, monkeypatch):
    monkeypatch.delenv("LECTERN_HOST", raising=False)
    every = "0.0.0.0"  # noqa: S104
    result, hosts = _serve_host(
        monkeypatch, "--host", every, "--public-url", "http://192.168.1.2:8765"
    )
    assert result.exit_code == 0, result.output
    assert hosts == [every]


def test_every_address_without_a_public_url_is_refused_before_listening(paths, monkeypatch):
    monkeypatch.setenv("LECTERN_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.delenv("LECTERN_PUBLIC_URL", raising=False)
    result, hosts = _serve_host(monkeypatch)
    assert result.exit_code == 2 and hosts == []
    assert "LECTERN_PUBLIC_URL" in result.output and "http://192.168.1.2:8765" in result.output


@pytest.mark.parametrize("host", ["192.168.1.2", "::", "localhost"])
def test_any_other_host_is_refused_before_listening(paths, monkeypatch, host):
    monkeypatch.setenv("LECTERN_HOST", host)
    monkeypatch.setenv("LECTERN_PUBLIC_URL", "http://192.168.1.2:8765")
    result, hosts = _serve_host(monkeypatch)
    assert result.exit_code == 2 and hosts == []
    assert "LECTERN_HOST must be 127.0.0.1" in result.output
