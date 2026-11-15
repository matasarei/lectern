"""Reading aloud over HTTP: everyone lists the voices, finds a reply's language and hears a
sentence; only an admin picks a language's voice, which downloads it."""

from __future__ import annotations

import io
import logging
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.catalog import catalog
from lectern.core import accounts, settings
from lectern.modules.voice import api as voice_api
from lectern.modules.voice.speak import Voices

LOCAL = "http://127.0.0.1:8765"
WEB = "https://lectern.example"
PASSWORD = "we walked to the lake in may"  # noqa: S105 (made up for the tests)
DENIS, DMITRI, LJSPEECH = "piper-ru-ru-denis", "piper-ru-ru-dmitri", "piper-en-us-ljspeech"


class Speaker:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.spoken: list[tuple[str, float]] = []

    def wav(self, text: str, speed: float) -> bytes:
        if self.fail:
            raise RuntimeError("onnxruntime refused")
        self.spoken.append((text, speed))
        out = io.BytesIO()
        with wave.open(out, "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(22050)
            file.writeframes(b"\0\0" * 100)
        return out.getvalue()


@pytest.fixture
def app(paths, monkeypatch):
    app = create_app(paths, public_urls=[WEB])
    app.state.started = []
    app.state.on_disk = set()
    app.state.loaded = []
    app.state.fail = False

    def start(entry):  # noqa: ANN001, ANN202 — no real download in a test
        app.state.started.append(entry.id)
        return app.state.downloads.state(entry)

    def load(model: Path, config: Path, speaker: int | None) -> Speaker:
        app.state.loaded.append(model.name)
        app.state.speaker = Speaker(app.state.fail)
        return app.state.speaker

    monkeypatch.setattr(app.state.downloads, "start", start)
    monkeypatch.setattr(
        app.state.downloads, "is_downloaded", lambda entry: entry.id in app.state.on_disk
    )
    app.state.voices = Voices(app.state.downloads.file_path, load=load)
    return app


@pytest.fixture
def restricted(monkeypatch):
    """A second Russian voice under a non-commercial licence."""
    real = catalog.load()
    other = real.get(DMITRI).model_copy(
        update={
            "id": "nc-voice",
            "title": "NC voice",
            "licence": "CC BY-NC-SA 4.0",
            "permissive": False,
        }
    )
    extended = real.model_copy(update={"models": [*real.models, other]})
    monkeypatch.setattr(catalog, "load", lambda: extended)
    return other


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


def _chosen(app) -> dict:  # noqa: ANN001
    return settings.get(app.state.sessions, settings.VOICE_KEY, settings.VOICE_DEFAULTS)["chosen"]


def _language(body: dict, code: str) -> dict:
    return next(language for language in body["languages"] if language["code"] == code)


def _say(client: TestClient, text: str = "Привет.", language: str = "ru", **more):  # noqa: ANN202
    return client.post("/api/voice/speak", json={"text": text, "language": language, **more})


# --- listing and choosing --------------------------------------------------------------------


def test_every_language_a_voice_speaks_is_listed_with_its_voices(app):
    body = _local(app).get("/api/voice").json()
    russian = _language(body, "ru")
    assert {"en", "ru"} <= {language["code"] for language in body["languages"]}
    assert (russian["voice"], russian["chosen"]) == (DENIS, None)  # the default, nothing picked
    marks = {v["id"]: (v["default"], v["licence_notice"]) for v in russian["voices"]}
    assert marks == {
        DENIS: (True, False),
        DMITRI: (False, False),
        "piper-ru-ru-irina": (False, True),  # the recordings' licence isn't known
        "piper-ru-ru-ruslan": (False, True),  # non-commercial
    }
    denis = russian["voices"][0]
    assert denis["installed"] is False and denis["download"]["status"] == "none"
    assert denis["licence_notice"] is False and denis["size"] > 60_000_000


def test_choosing_a_voice_saves_it_and_downloads_it(app):
    answer = _local(app).put("/api/voice/ru", json={"model_id": DMITRI})
    assert answer.status_code == 200
    assert _language(answer.json(), "ru")["voice"] == DMITRI
    assert _chosen(app) == {"ru": DMITRI}
    assert app.state.started == [DMITRI]


def test_choosing_the_default_again_stores_no_choice_and_a_voice_on_disk_isnt_downloaded(app):
    client = _local(app)
    client.put("/api/voice/ru", json={"model_id": DMITRI})
    app.state.on_disk.add(DENIS)

    answer = client.put("/api/voice/ru", json={"model_id": DENIS})

    assert _language(answer.json(), "ru")["voice"] == DENIS
    assert _chosen(app) == {}
    assert app.state.started == [DMITRI]  # Denis is on disk already


@pytest.mark.parametrize(
    ("code", "model_id", "status"),
    [("xx", DENIS, 404), ("ru", LJSPEECH, 409), ("ru", "qwen3.5-4b", 409), ("ru", "nope", 409)],
)
def test_a_voice_that_cannot_be_chosen_is_refused_and_nothing_is_stored(
    app, code, model_id, status
):
    assert _local(app).put(f"/api/voice/{code}", json={"model_id": model_id}).status_code == status
    assert _chosen(app) == {} and app.state.started == []


def test_a_restricted_voice_needs_its_licence_accepted(app, restricted):
    client = _local(app)
    refused = client.put("/api/voice/ru", json={"model_id": "nc-voice"})
    assert refused.status_code == 409 and "CC BY-NC-SA 4.0" in refused.json()["detail"]
    assert _chosen(app) == {}

    accepted = client.put("/api/voice/ru", json={"model_id": "nc-voice", "accept_licence": True})
    assert accepted.status_code == 200 and _chosen(app) == {"ru": "nc-voice"}


def test_a_member_lists_and_listens_but_only_an_admin_chooses(app):
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    accounts.add_user(app.state.sessions, "olena", PASSWORD)
    app.state.on_disk.add(DENIS)
    member = _signed_in(app, "olena")

    assert member.get("/api/voice").status_code == 200
    assert _say(member).status_code == 200
    assert member.put("/api/voice/ru", json={"model_id": DMITRI}).status_code == 403
    assert _chosen(app) == {}
    assert (
        _signed_in(app, "yevhen").put("/api/voice/ru", json={"model_id": DMITRI}).status_code == 200
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/voice", None),
        ("put", "/api/voice/ru", {"model_id": DMITRI}),
        ("post", "/api/voice/language", {"text": "Привет"}),
        ("post", "/api/voice/speak", {"text": "Привет", "language": "ru"}),
    ],
)
def test_every_route_needs_the_token(app, method, path, body):
    client = TestClient(app, base_url=LOCAL, headers={"Origin": LOCAL})
    answer = client.request(method, path, json=body)
    assert answer.status_code == 401


# --- the language of a reply ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Встреча перенесена на пятницу, возьмите договор.", "ru"),
        ("The meeting is moved to Friday; bring the lease.", "en"),
        ("42 + 17", None),
    ],
)
def test_a_reply_is_found_in_a_language_a_voice_speaks(app, text, language):
    answer = _local(app).post("/api/voice/language", json={"text": text})
    assert answer.status_code == 200 and answer.json() == {"language": language}


def test_a_reply_too_long_to_look_at_is_refused(app):
    text = "a" * (voice_api.MAX_REPLY + 1)
    assert _local(app).post("/api/voice/language", json={"text": text}).status_code == 422


# --- speaking ---------------------------------------------------------------------------------


def test_a_sentence_is_spoken_as_wav_by_the_languages_voice(app):
    app.state.on_disk.add(DENIS)

    answer = _say(_local(app), "Привет, мир.", speed=1.5)

    assert answer.status_code == 200
    assert answer.headers["content-type"] == "audio/wav"
    assert answer.headers["cache-control"] == "no-store"
    with wave.open(io.BytesIO(answer.content)) as read:
        assert read.getframerate() == 22050
    assert app.state.loaded == ["ru_RU-denis-medium.onnx"]
    assert app.state.speaker.spoken == [("Привет, мир.", 1.5)]


def test_the_chosen_voice_speaks(app):
    client = _local(app)
    client.put("/api/voice/ru", json={"model_id": DMITRI})
    app.state.on_disk.add(DMITRI)
    assert _say(client).status_code == 200
    assert app.state.loaded == ["ru_RU-dmitri-medium.onnx"]


def test_a_voice_not_on_disk_says_so(app):
    answer = _say(_local(app))
    assert answer.status_code == 409
    assert answer.json()["detail"] == "The Russian voice isn't on this computer yet."
    assert app.state.loaded == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"text": "Hallo", "language": "xx"}, 422),
        ({"text": "", "language": "ru"}, 422),
        ({"text": "a" * (voice_api.MAX_SENTENCE + 1), "language": "ru"}, 422),
        ({"text": "Привет", "language": "ru", "speed": 3}, 422),
        ({"text": "Привет", "language": "ru", "speed": 0.1}, 422),
    ],
)
def test_a_sentence_that_cannot_be_spoken_is_refused(app, body, status):
    app.state.on_disk.add(DENIS)
    assert _local(app).post("/api/voice/speak", json=body).status_code == status
    assert app.state.loaded == []


def test_a_voice_that_fails_answers_503_and_keeps_the_text_out_of_the_log(app, caplog):
    app.state.on_disk.add(DENIS)
    app.state.fail = True
    with caplog.at_level(logging.DEBUG):
        answer = _say(_local(app), "Мой секретный пароль.")
    assert answer.status_code == 503
    assert answer.json()["detail"] == "Piper Denis (Russian, RU) couldn't speak this text."
    assert "секретный" not in caplog.text
