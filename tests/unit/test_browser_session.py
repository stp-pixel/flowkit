import asyncio

import pytest

from agent import config
from agent.browser import session as session_mod
from agent.browser.actions import ClickStep, StepResult
from agent.browser.cdp import CdpError, PageTarget
from agent.browser.session import BrowserSession
from agent.browser.snapshot import Action, Snapshot

FLOW = "https://flow.google.com/project/abc"


class FakeClient:
    def __init__(self, href=FLOW):
        self.closed = False
        self.href = href
        self.close_calls = 0
        self.close_raises = False

    async def evaluate(self, expr):
        if self.closed:
            raise CdpError("CDP_CLOSED", "closed")
        return self.href

    async def close(self):
        self.close_calls += 1
        self.closed = True
        if self.close_raises:
            raise RuntimeError("boom")


class Counter:
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    async def __call__(self, *a, **k):
        self.calls += 1
        return await self.fn(*a, **k)


def pages_fn(pages=None, exc=None):
    async def fn(port):
        if exc:
            raise exc
        return pages if pages is not None else [PageTarget("1", FLOW, "t", "ws://x/1")]
    return fn


def snap_result(url=FLOW):
    return Snapshot(url, "T", "txt", [Action("e1", "button", "button", "Go")], 0, False, False)


def make(port=9333, ws_port=9222, pages=None, list_exc=None, clients=None, snaps=None,
         perform=None):
    clients = clients if clients is not None else []

    async def connect(ws, t):
        c = FakeClient()
        clients.append(c)
        return c

    snap_seq = list(snaps or [])

    async def take(client):
        if snap_seq:
            item = snap_seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return snap_result()

    async def default_perform(client, step, hosts):
        return StepResult(step.op, True)

    connect_fn, take_fn = Counter(connect), Counter(take)
    perform_fn = Counter(perform or default_perform)
    s = BrowserSession(
        port, ["flow.google.com"], "", 5, ws_port=ws_port,
        list_pages_fn=pages_fn(pages, list_exc), connect_fn=connect_fn,
        take_snapshot_fn=take_fn, perform_fn=perform_fn,
    )
    return s, connect_fn, take_fn, perform_fn, clients


def click(t):
    return ClickStep(op="click", target=t)


async def test_port_equal_to_ws_port_is_conflict():
    s, *_ = make(port=9222, ws_port=9222)
    st = await s.status()
    assert st["error"] == "PORT_CONFLICT" and "--remote-debugging-port=9333" in st["hint"]


async def test_status_unreachable_does_not_raise():
    s, *_ = make(list_exc=CdpError("CDP_UNREACHABLE", "no", hint="start it"))
    st = await s.status()
    assert st["reachable"] is False and st["error"] == "CDP_UNREACHABLE" and st["hint"]


async def test_status_wraps_non_cdp_errors():
    s, *_ = make(list_exc=ValueError("bad json"))
    st = await s.status()
    assert st["error"] == "CDP_UNREACHABLE" and "--remote-debugging-port=9333" in st["hint"]
    with pytest.raises(CdpError) as ei:
        await s.snapshot()
    assert ei.value.code == "CDP_UNREACHABLE"


async def test_status_reports_page_url():
    s, *_ = make()
    st = await s.status()
    assert st["reachable"] and st["page_url"].startswith("https://flow.google.com/")


async def test_pages_without_ws_url_are_skipped_or_reported():
    pages = [PageTarget("1", FLOW, "t", ""), PageTarget("2", FLOW + "2", "t", "ws://x/2")]
    s, connect_fn, *_ = make(pages=pages)
    seen = []
    orig = s._connect

    async def spy(ws, t):
        seen.append(ws)
        return await orig(ws, t)

    s._connect = spy
    await s.snapshot()
    assert seen == ["ws://x/2"]
    s2, *_ = make(pages=[PageTarget("1", FLOW, "t", "")])
    st = await s2.status()
    assert st["error"] == "NO_FLOW_PAGE" and "DevTools" in st["hint"]


async def test_devtools_hint_even_when_other_page_has_ws_url():
    pages = [PageTarget("0", "chrome://newtab/", "n", "ws://x/0"),
             PageTarget("1", FLOW, "t", "")]
    s, *_ = make(pages=pages)
    st = await s.status()
    assert st["error"] == "NO_FLOW_PAGE" and "DevTools" in st["hint"]


async def test_snapshot_on_disallowed_host_reveals_url_only():
    s, _, take_fn, _, clients = make()
    await s.snapshot()
    clients[0].href = "https://accounts.google.com/signin"
    r = await s.snapshot()
    assert r["actions"] == []
    assert r["table"] == "url: https://accounts.google.com/signin\n(host not allowed — no actions)"
    assert take_fn.calls == 1


async def test_snapshot_returns_table_and_actions():
    s, *_ = make()
    r = await s.snapshot()
    assert r["url"] == FLOW and r["table"].startswith("url: ")
    assert r["actions"][0]["id"] == "e1"


async def test_act_stops_at_first_failure():
    seq = [StepResult("click", True), StepResult("click", False, "OBSCURED")]

    async def perform(c, step, hosts):
        return seq.pop(0)

    s, _, _, perform_fn, _ = make(perform=perform)
    r = await s.act([click("e1"), click("e2"), click("e3")])
    assert r["completed"] == 1 and len(r["results"]) == 2 and perform_fn.calls == 2


async def test_act_does_not_replay_step_after_disconnect():
    clients_ref = []

    async def perform(c, step, hosts):
        c.closed = True
        return StepResult("click", False, "CDP_CLOSED")

    s, connect_fn, take_fn, perform_fn, clients = make(perform=perform, clients=clients_ref)
    r = await s.act([click("e3"), click("e4")])
    assert perform_fn.calls == 1 and r["results"][0]["error"] == "CDP_CLOSED"
    assert connect_fn.calls == 1
    assert r["table"] is None and r["url"] == FLOW and take_fn.calls == 0


async def test_next_call_reconnects_after_disconnect():
    async def perform(c, step, hosts):
        c.closed = True
        return StepResult("click", False, "CDP_CLOSED")

    s, connect_fn, *_ = make(perform=perform)
    await s.act([click("e3")])
    await s.snapshot()
    assert connect_fn.calls == 2


async def test_snapshot_retries_once_on_closed():
    s, connect_fn, *_ = make(snaps=[CdpError("CDP_CLOSED", "gone"), snap_result()])
    r = await s.snapshot()
    assert connect_fn.calls == 2 and r["table"].startswith("url: ")


async def test_snapshot_gives_up_after_second_closed():
    s, connect_fn, *_ = make(snaps=[CdpError("CDP_CLOSED", "a"), CdpError("CDP_CLOSED", "b")])
    with pytest.raises(CdpError) as ei:
        await s.snapshot()
    assert ei.value.code == "CDP_CLOSED" and connect_fn.calls == 2


async def test_drop_client_swallows_close_errors():
    s, _, _, _, clients = make()
    await s.snapshot()
    clients[0].close_raises = True
    await s._drop_client()
    assert s._client is None and clients[0].close_calls == 1


async def test_calls_are_serialized():
    active = 0
    overlap = False

    async def perform(c, step, hosts):
        nonlocal active, overlap
        active += 1
        overlap = overlap or active > 1
        await asyncio.sleep(0.01)
        active -= 1
        return StepResult(step.op, True)

    s, *_ = make(perform=perform)
    await asyncio.gather(s.act([click("e1")]), s.act([click("e2")]))
    assert overlap is False


def test_singleton_uses_config(monkeypatch):
    monkeypatch.setattr(session_mod, "_session", None)
    monkeypatch.setattr(config, "FLOW_PROJECT_ID", "proj-123")
    s = session_mod.get_browser_session()
    assert s._prefer == "proj-123" and s._ws_port == config.WS_PORT
    assert s._port == config.FLOW_CDP_PORT
    assert session_mod.get_browser_session() is s


def _scripted_perform(seq):
    async def perform(c, step, hosts):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return perform


async def test_act_timeout_mid_batch_keeps_results_and_drops_client():
    seq = [StepResult("click", True), CdpError("CDP_TIMEOUT", "slow"), StepResult("click", True)]
    s, connect_fn, _, perform_fn, _ = make(perform=_scripted_perform(seq))
    r = await s.act([click("e1"), click("e2"), click("e3")])
    assert r["completed"] == 1 and perform_fn.calls == 2
    assert [x["error"] for x in r["results"]] == [None, "CDP_TIMEOUT"]
    await s.snapshot()
    assert connect_fn.calls == 2


async def test_act_js_error_mid_batch_keeps_client():
    seq = [StepResult("click", True), CdpError("JS_ERROR", "bad")]
    s, connect_fn, _, perform_fn, _ = make(perform=_scripted_perform(seq))
    r = await s.act([click("e1"), click("e2"), click("e3")])
    assert r["completed"] == 1 and r["results"][1]["error"] == "JS_ERROR"
    assert perform_fn.calls == 2
    await s.snapshot()
    assert connect_fn.calls == 1


async def test_act_final_snapshot_failure_keeps_results():
    s, *_ = make(snaps=[CdpError("CDP_TIMEOUT", "slow")])
    r = await s.act([click("e1"), click("e2")])
    assert r["completed"] == 2 and r["table"] is None and r["table_error"] == "CDP_TIMEOUT"


async def test_snapshot_redirect_between_href_and_snapshot_reveals_url_only():
    # location.href said flow.google.com, but the page had moved by the time the
    # snapshot ran: nothing from the off-host page may leak.
    leaked = Snapshot("https://accounts.google.com/signin", "Sign in", "secret text",
                      [Action("e1", "fill", "textbox", "Email")], 0, False, False)
    s, *_ = make(snaps=[leaked])
    r = await s.snapshot()
    assert r["url"] == "https://accounts.google.com/signin"
    assert r["actions"] == []
    assert r["table"] == "url: https://accounts.google.com/signin\n(host not allowed — no actions)"


async def test_act_final_table_redirect_reveals_url_only():
    leaked = Snapshot("https://accounts.google.com/signin", "Sign in", "secret text",
                      [Action("e1", "fill", "textbox", "Email")], 0, False, False)
    s, *_ = make(snaps=[leaked])
    r = await s.act([click("e1")])
    assert r["url"] == "https://accounts.google.com/signin"
    assert r["table"] == "url: https://accounts.google.com/signin\n(host not allowed — no actions)"


async def test_act_unexpected_exception_is_internal_and_drops_client():
    seq = [StepResult("click", True), KeyError("x"), StepResult("click", True)]
    s, connect_fn, take_fn, perform_fn, clients = make(perform=_scripted_perform(seq))
    r = await s.act([click("e1"), click("e2"), click("e3")])
    assert perform_fn.calls == 2 and r["completed"] == 1
    assert r["results"][1] == {"op": "click", "ok": False, "error": "INTERNAL",
                               "detail": "'x'"}
    assert r["table"] is None and take_fn.calls == 0
    assert clients[0].close_calls == 1 and s._client is None
    await s.snapshot()
    assert connect_fn.calls == 2
