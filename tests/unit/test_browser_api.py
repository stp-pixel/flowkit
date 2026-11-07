"""/api/browser/*: gated by FLOW_CDP_ENABLED, steps validated at the boundary."""

import pytest
from fastapi.testclient import TestClient

import agent.main as main
from agent import config
from agent.browser.cdp import CdpError

WAIT = {"op": "wait", "ms": 0}


class FakeSession:
    def __init__(self, error=None):
        self.error = error
        self.steps = None
        self.kwargs = None

    async def status(self):
        return {"enabled": True}

    async def snapshot(self, text_chars=600):
        if self.error:
            raise self.error
        self.kwargs = {"text_chars": text_chars}
        return {"url": "https://flow.google.com/x", "table": "url: https://flow.google.com/x\n", "actions": []}

    async def act(self, steps, snapshot=True, text_chars=600):
        if self.error:
            raise self.error
        self.steps = steps
        self.kwargs = {"snapshot": snapshot, "text_chars": text_chars}
        return {"results": [{"op": s.op, "ok": True} for s in steps], "completed": len(steps),
                "url": "u", "table": "t", "table_error": "boom"}


@pytest.fixture
def client():
    return TestClient(main.app, base_url="http://127.0.0.1:8100")


@pytest.fixture
def fake(monkeypatch):
    s = FakeSession()
    monkeypatch.setattr("agent.api.browser.get_browser_session", lambda: s)
    monkeypatch.setattr(config, "FLOW_CDP_ENABLED", True)
    return s


def test_routes_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "FLOW_CDP_ENABLED", False)
    for r in (client.get("/api/browser/status"), client.get("/api/browser/snapshot"),
              client.post("/api/browser/act", json={"steps": [WAIT]})):
        assert r.status_code == 404
        assert r.json()["detail"] == "browser control disabled; set FLOW_CDP_ENABLED=1"


def test_status(client, fake):
    assert client.get("/api/browser/status").json() == {"enabled": True}


def test_snapshot_returns_table(client, fake):
    r = client.get("/api/browser/snapshot?text_chars=50")
    assert r.status_code == 200 and r.json()["table"].startswith("url: ")
    assert fake.kwargs == {"text_chars": 50}


@pytest.mark.parametrize("n", [-1, 6001])
def test_snapshot_text_chars_bounds(client, fake, n):
    assert client.get(f"/api/browser/snapshot?text_chars={n}").status_code == 422


def test_act_passes_steps_and_returns_results(client, fake):
    r = client.post("/api/browser/act", json={"steps": [
        {"op": "fill", "target": "e1", "text": "hi"}, {"op": "click", "target": "e2"}]})
    assert r.status_code == 200
    assert [s.op for s in fake.steps] == ["fill", "click"]
    assert fake.kwargs == {"snapshot": True, "text_chars": 600}
    assert r.json()["completed"] == 2 and r.json()["table_error"] == "boom"


def test_act_options_forwarded(client, fake):
    client.post("/api/browser/act", json={"steps": [WAIT], "snapshot": False, "text_chars": 6000})
    assert fake.kwargs == {"snapshot": False, "text_chars": 6000}


@pytest.mark.parametrize("steps", [[], [WAIT] * 21])
def test_act_rejects_empty_and_oversized_batches(client, fake, steps):
    assert client.post("/api/browser/act", json={"steps": steps}).status_code == 422


def test_act_rejects_unknown_op(client, fake):
    assert client.post("/api/browser/act", json={"steps": [{"op": "eval"}]}).status_code == 422


def test_act_rejects_misspelled_step_field(client, fake):
    step = {"op": "wait_for_text", "text": "x", "timout_s": 5}
    assert client.post("/api/browser/act", json={"steps": [step]}).status_code == 422


def test_act_rejects_unknown_top_level_key(client, fake):
    assert client.post("/api/browser/act", json={"steps": [WAIT], "bogus": 1}).status_code == 422


def test_act_text_chars_bounds(client, fake):
    for n in (-1, 6001):
        assert client.post("/api/browser/act", json={"steps": [WAIT], "text_chars": n}).status_code == 422


@pytest.mark.parametrize("code,status", [
    ("CDP_UNREACHABLE", 503), ("CDP_CLOSED", 503), ("CDP_TIMEOUT", 504),
    ("NO_FLOW_PAGE", 409), ("PORT_CONFLICT", 409), ("JS_ERROR", 502), ("CDP_PROTOCOL", 502),
])
def test_cdp_error_mapping(client, fake, code, status):
    fake.error = CdpError(code, "msg", "do this")
    for r in (client.get("/api/browser/snapshot"),
              client.post("/api/browser/act", json={"steps": [WAIT]})):
        assert r.status_code == status
        assert r.json()["detail"] == {"code": code, "message": "msg", "hint": "do this"}


def test_cross_site_browser_call_refused(client, fake):
    r = client.post("/api/browser/act", headers={"origin": "https://evil.example"},
                    json={"steps": [WAIT]})
    assert r.status_code == 403
