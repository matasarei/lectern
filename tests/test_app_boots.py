from fastapi.testclient import TestClient

from lectern import __version__
from lectern.app import create_app


def test_health_answers(paths):
    # The security layer only answers on 127.0.0.1/localhost, so the client must use that host.
    client = TestClient(create_app(paths), base_url="http://127.0.0.1:8765")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_home_comes_from_env(paths):
    from lectern.config import get_paths

    assert get_paths().home == paths.home
