"""The Languages page's API: everyone sees the languages, only an admin adds or removes one."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.catalog import catalog
from lectern.core import accounts, settings

LOCAL = "http://127.0.0.1:8765"
WEB = "https://lectern.example"
PASSWORD = "we walked to the lake in may"  # noqa: S105 (made up for the tests)
SALAMANDRA = "salamandrata-2b"


@pytest.fixture
def translator(monkeypatch):
    """A second translation model, under a restricted licence."""
    real = catalog.load()
    other = real.get(SALAMANDRA).model_copy(
        update={
            "id": "other-mt",
            "title": "Other MT",
            "licence": "Gemma Terms of Use",
            "permissive": False,
            "notes": "Read the Gemma terms before using it for work.",
        }
    )
    extended = real.model_copy(update={"models": [*real.models, other]})
    monkeypatch.setattr(catalog, "load", lambda: extended)
    return other


@pytest.fixture
def app(paths, monkeypatch):
    monkeypatch.setenv("LECTERN_TIER", "minimum")
    app = create_app(paths, public_urls=[WEB])
    app.state.started = []
    app.state.on_disk = set()

    def start(entry):  # noqa: ANN001, ANN202 — no real download in a test
        app.state.started.append(entry.id)
        return app.state.downloads.state(entry)

    monkeypatch.setattr(app.state.downloads, "start", start)
    monkeypatch.setattr(
        app.state.downloads, "is_downloaded", lambda entry: entry.id in app.state.on_disk
    )
    return app


def _local(app) -> TestClient:  # noqa: ANN001
    return TestClient(
        app, base_url=LOCAL, headers={"X-Lectern-Token": app.state.token, "Origin": LOCAL}
    )


def _signed_in(app, login: str) -> TestClient:  # noqa: ANN001
    browser = TestClient(app, base_url=WEB, headers={"Origin": WEB})
    answer = browser.post("/api/login", json={"login": login, "password": PASSWORD})
    assert answer.status_code == 200, answer.text
    browser.headers["X-Lectern-Token"] = answer.json()["token"]
    return browser


def _stored(app) -> dict:  # noqa: ANN001
    return settings.get(app.state.sessions, settings.LANGUAGES_KEY, settings.LANGUAGE_DEFAULTS)[
        "added"
    ]


def _language(body: dict, code: str) -> dict:
    return next(language for language in body["languages"] if language["code"] == code)


def test_every_language_a_model_translates_is_listed_with_its_options(app):
    body = _local(app).get("/api/languages").json()
    assert body["tier"] == "minimum"
    codes = [language["code"] for language in body["languages"]]
    assert "uk" in codes and "de" in codes and codes == sorted(codes)
    ukrainian = _language(body, "uk")
    assert ukrainian["added"] is None
    option, other = ukrainian["options"]  # the best first
    assert (option["id"], other["id"]) == (SALAMANDRA, "hy-mt2-1.8b")
    assert (option["score"], other["score"], ukrainian["chat_score"]) == (60.0, 55.0, 50.0)
    assert other["best"] is False and other["open_licence"] is False
    assert option["best"] is True and option["open_licence"] is True
    assert option["licence_notice"] is False
    assert option["download"]["status"] == "none"  # nothing of it on disk


def test_the_best_and_the_open_licence_option_are_marked(app, translator):
    options = _language(_local(app).get("/api/languages").json(), "uk")["options"]
    marks = {o["id"]: (o["best"], o["open_licence"], o["licence_notice"]) for o in options}
    assert marks == {
        SALAMANDRA: (True, True, False),
        "hy-mt2-1.8b": (False, False, False),
        "other-mt": (False, False, True),
    }


def test_adding_a_language_saves_it_and_downloads_its_model(app):
    client = _local(app)
    answer = client.put("/api/languages/uk", json={"model_id": SALAMANDRA})
    assert answer.status_code == 200
    assert _language(answer.json(), "uk")["added"] == {"model": SALAMANDRA, "previous": None}
    assert _stored(app) == {"uk": {"model": SALAMANDRA, "previous": None}}
    assert app.state.started == [SALAMANDRA]


def test_a_restricted_model_needs_its_licence_accepted(app, translator):
    client = _local(app)
    refused = client.put("/api/languages/uk", json={"model_id": "other-mt"})
    assert refused.status_code == 409
    assert "Gemma Terms of Use" in refused.json()["detail"]
    assert _stored(app) == {} and app.state.started == []
    accepted = client.put(
        "/api/languages/uk", json={"model_id": "other-mt", "accept_licence": True}
    )
    assert accepted.status_code == 200
    assert _stored(app) == {"uk": {"model": "other-mt", "previous": None}}


def test_a_new_model_keeps_the_one_on_disk_translating_until_it_is_downloaded(app, translator):
    client = _local(app)
    client.put("/api/languages/uk", json={"model_id": SALAMANDRA})
    app.state.on_disk.add(SALAMANDRA)
    client.put("/api/languages/uk", json={"model_id": "other-mt", "accept_licence": True})
    assert _stored(app) == {"uk": {"model": "other-mt", "previous": SALAMANDRA}}
    # Back to Salamandra before the other one finished: nothing is left waiting on it.
    client.put("/api/languages/uk", json={"model_id": SALAMANDRA})
    assert _stored(app) == {"uk": {"model": SALAMANDRA, "previous": None}}


def test_changing_twice_before_a_download_ends_keeps_the_model_on_disk(app, translator):
    client = _local(app)
    client.put("/api/languages/uk", json={"model_id": "other-mt", "accept_licence": True})
    client.put("/api/languages/uk", json={"model_id": SALAMANDRA})  # nothing on disk yet
    assert _stored(app) == {"uk": {"model": SALAMANDRA, "previous": None}}


def test_the_same_model_again_only_restarts_its_download(app):
    client = _local(app)
    client.put("/api/languages/uk", json={"model_id": SALAMANDRA})
    client.put("/api/languages/uk", json={"model_id": SALAMANDRA})
    assert _stored(app) == {"uk": {"model": SALAMANDRA, "previous": None}}
    assert app.state.started == [SALAMANDRA, SALAMANDRA]


@pytest.mark.parametrize(
    ("code", "model_id", "status"),
    [
        ("sw", SALAMANDRA, 404),  # no model translates it
        ("ja", SALAMANDRA, 409),  # some do, not this one
        ("uk", "gone-mt", 404),  # not in the catalog
        ("uk", "qwen3.5-4b", 409),  # in the catalog, but not a translation model
    ],
)
def test_a_language_or_model_that_isnt_on_offer_is_refused(app, code, model_id, status):
    answer = _local(app).put(f"/api/languages/{code}", json={"model_id": model_id})
    assert answer.status_code == status
    assert _stored(app) == {} and app.state.started == []


def test_a_model_too_big_for_this_computer_isnt_on_offer(app, translator, monkeypatch):
    real = catalog.load()
    big = real.get("other-mt").model_copy(update={"ctx": {"comfortable": 8192}})
    smaller = real.model_copy(
        update={"models": [m if m.id != "other-mt" else big for m in real.models]}
    )
    monkeypatch.setattr(catalog, "load", lambda: smaller)
    client = _local(app)
    options = _language(client.get("/api/languages").json(), "uk")["options"]
    ids = [o["id"] for o in options]
    assert SALAMANDRA in ids and "other-mt" not in ids
    refused = client.put("/api/languages/uk", json={"model_id": "other-mt", "accept_licence": True})
    assert refused.status_code == 409


def test_removing_a_language_leaves_its_model(app):
    client = _local(app)
    client.put("/api/languages/uk", json={"model_id": SALAMANDRA})
    removed = client.delete("/api/languages/uk")
    assert removed.status_code == 200
    assert _language(removed.json(), "uk")["added"] is None
    assert _stored(app) == {}
    assert client.delete("/api/languages/uk").status_code == 404


def test_models_and_memory_say_an_added_languages_model_translates(app):
    client = _local(app)
    client.put("/api/languages/uk", json={"model_id": SALAMANDRA})
    models = client.get("/api/models").json()["models"]
    assert next(m for m in models if m["id"] == SALAMANDRA)["used_by"] == ["translate"]


def test_a_member_sees_the_languages_but_only_an_admin_changes_them(app):
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    accounts.add_user(app.state.sessions, "olena", PASSWORD)
    admin, member = _signed_in(app, "yevhen"), _signed_in(app, "olena")
    assert admin.put("/api/languages/uk", json={"model_id": SALAMANDRA}).status_code == 200
    seen = member.get("/api/languages")
    assert seen.status_code == 200
    assert _language(seen.json(), "uk")["added"]["model"] == SALAMANDRA
    assert member.put("/api/languages/de", json={"model_id": SALAMANDRA}).status_code == 403
    assert member.delete("/api/languages/uk").status_code == 403
    assert _stored(app) == {"uk": {"model": SALAMANDRA, "previous": None}}


def test_someone_not_signed_in_gets_nothing(app):
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    stranger = TestClient(app, base_url=WEB, headers={"Origin": WEB})
    assert stranger.get("/api/languages").status_code == 401
    assert stranger.put("/api/languages/uk", json={"model_id": SALAMANDRA}).status_code == 401
