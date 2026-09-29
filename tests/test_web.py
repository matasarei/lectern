from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def dist(tmp_path):
    dist = tmp_path / "web_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Lectern</title>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path / "secret.txt").write_text("not for the browser")
    return dist


@pytest.fixture
def client(paths, dist):
    return TestClient(create_app(paths, web_dist=dist), base_url=BASE)


def test_root_serves_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>Lectern</title>" in response.text


def test_assets_are_served(client):
    response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_client_side_routes_get_index(client):
    assert "<title>Lectern</title>" in client.get("/settings/models").text


def test_unknown_api_path_is_404_not_index(client, paths):
    app = client.app
    response = client.get("/api/nope", headers={"X-Lectern-Token": app.state.token})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


def test_paths_cannot_escape_the_dist_folder(client):
    response = client.get("/assets/../../secret.txt")
    assert "not for the browser" not in response.text


def test_web_pages_carry_the_csp(client):
    assert "default-src 'self'" in client.get("/").headers["content-security-policy"]


def test_missing_build_explains_itself(paths, tmp_path):
    client = TestClient(create_app(paths, web_dist=tmp_path / "missing"), base_url=BASE)
    response = client.get("/")
    assert response.status_code == 503
    assert "npm --prefix web run build" in response.text
