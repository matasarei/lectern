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
