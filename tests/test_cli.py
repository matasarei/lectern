from __future__ import annotations

import socket

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
