"""The public demo caps how many LLM-backed messages one visitor, and everyone together, can send."""

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from app import ratelimit


@pytest.fixture()
def client():
    ratelimit.reset()
    app = FastAPI()

    @app.post("/send", dependencies=[Depends(ratelimit.check)])
    def send() -> dict:
        return {"ok": True}

    yield TestClient(app)
    ratelimit.reset()


def test_one_visitor_is_capped(client, monkeypatch):
    monkeypatch.setenv("DEMO_MESSAGES_PER_VISITOR", "3")
    ip = {"x-forwarded-for": "203.0.113.5"}
    assert [client.post("/send", headers=ip).status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.post("/send", headers=ip)
    assert blocked.status_code == 429
    assert "Coba lagi dalam" in blocked.json()["detail"]
    # another visitor still gets through
    assert client.post("/send", headers={"x-forwarded-for": "198.51.100.7"}).status_code == 200


def test_everyone_together_is_capped(client, monkeypatch):
    monkeypatch.setenv("DEMO_MESSAGES_PER_VISITOR", "0")
    monkeypatch.setenv("DEMO_MESSAGES_PER_HOUR", "2")
    codes = [client.post("/send", headers={"x-forwarded-for": f"203.0.113.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_zero_turns_caps_off(client, monkeypatch):
    monkeypatch.setenv("DEMO_MESSAGES_PER_VISITOR", "0")
    monkeypatch.setenv("DEMO_MESSAGES_PER_HOUR", "0")
    assert all(client.post("/send").status_code == 200 for _ in range(50))
