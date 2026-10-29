"""Recorded runs replay through the same event stream, labelled as replays."""

import json

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from web import server  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    events = [
        {"t": 0.0, "event": {"type": "hello", "online": True, "idea": "a PPG front-end"}},
        {"t": 0.5, "event": {"type": "stage", "agent": "scout", "status": "running"}},
        {"t": 1.0, "event": {"type": "done", "plan": {}}},
    ]
    (tmp_path / "ppg-live.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    (tmp_path / "broken.jsonl").write_text("not json\n")
    monkeypatch.setattr(server, "REPLAYS", tmp_path)
    return TestClient(server.app)


def test_lists_only_readable_recordings(client):
    got = client.get("/api/replays").json()
    assert [r["name"] for r in got] == ["ppg-live"]
    assert got[0]["online"] is True and got[0]["idea"] == "a PPG front-end"


def test_replay_is_labelled_then_streams_the_recording(client):
    body = client.get("/api/replay?name=ppg-live&speed=60").text
    data = [json.loads(l[5:]) for l in body.splitlines()
            if l.startswith("data: {") and l != "data: {}"]
    assert data[0]["type"] == "replay" and data[0]["name"] == "ppg-live"
    assert [e["type"] for e in data[1:]] == ["hello", "stage", "done"]
    assert "event: end" in body


@pytest.mark.parametrize("name", ["../etc/passwd", "nope"])
def test_rejects_unknown_or_unsafe_names(client, name):
    assert client.get("/api/replay", params={"name": name}).status_code in (404, 422)
