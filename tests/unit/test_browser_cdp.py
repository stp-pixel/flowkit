import asyncio
import importlib
import json

import httpx
import pytest
import websockets

from agent import config
from agent.browser.cdp import (
    CdpClient,
    CdpError,
    PageTarget,
    host_allowed,
    list_pages,
    pick_page,
)


def P(url):
    return PageTarget(id=url, url=url, title="t", ws_url="ws://x/" + url)


@pytest.fixture
def cdp_defaults(monkeypatch):
    for k in (
        "FLOW_CDP_ENABLED",
        "FLOW_CDP_PORT",
        "FLOW_CDP_ALLOWED_HOSTS",
        "FLOW_CDP_TIMEOUT_S",
    ):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(config)
    yield config
    monkeypatch.undo()
    importlib.reload(config)


def test_config_defaults(cdp_defaults):
    c = cdp_defaults
    assert c.FLOW_CDP_ENABLED is False
    assert c.FLOW_CDP_PORT == 9333 != c.WS_PORT
    assert c.FLOW_CDP_ALLOWED_HOSTS == ["flow.google.com"]
    assert c.FLOW_CDP_TIMEOUT_S == 10.0


async def test_list_pages_keeps_only_page_targets():
    body = [
        {"type": "page", "id": "A", "url": "https://flow.google.com/p/1",
         "title": "Flow", "webSocketDebuggerUrl": "ws://x/A"},
        {"type": "service_worker", "id": "B", "url": "https://flow.google.com/sw",
         "title": "sw", "webSocketDebuggerUrl": "ws://x/B"},
        {"type": "iframe", "id": "C", "url": "https://flow.google.com/f",
         "title": "f", "webSocketDebuggerUrl": "ws://x/C"},
    ]
    t = httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    pages = await list_pages(9333, transport=t)
    assert [p.id for p in pages] == ["A"]
    assert pages[0].ws_url == "ws://x/A"


async def test_list_pages_unreachable_hint_names_port():
    def boom(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(CdpError) as e:
        await list_pages(9333, transport=httpx.MockTransport(boom))
    assert e.value.code == "CDP_UNREACHABLE"
    assert "--remote-debugging-port=9333" in e.value.hint
    assert "--user-data-dir" in e.value.hint


def test_pick_page_filters_host_and_prefers_project():
    pages = [
        P("https://accounts.google.com/x"),
        P("https://flow.google.com/project/aaa"),
        P("https://flow.google.com/project/bbb"),
        P("https://flow.google.com.evil.example/"),
    ]
    assert pick_page(pages, ["flow.google.com"], "bbb").url.endswith("/bbb")
    assert pick_page(pages, ["flow.google.com"]).url.endswith("/aaa")


def test_pick_page_none_allowed_raises_no_flow_page():
    with pytest.raises(CdpError) as e:
        pick_page([P("https://example.com/")], ["flow.google.com"])
    assert e.value.code == "NO_FLOW_PAGE"


def test_host_allowed_is_exact_hostname():
    assert host_allowed("https://flow.google.com/x", ["flow.google.com"])
    assert not host_allowed("https://flow.google.com.evil.example/", ["flow.google.com"])
    assert not host_allowed("about:blank", ["flow.google.com"])


async def _serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


@pytest.fixture
async def fake_cdp():
    """Replies to each request with an event first, then the result; two
    concurrent requests are answered out of order."""
    async def handler(ws):
        held = []
        async for raw in ws:
            msg = json.loads(raw)
            held.append(msg)
            if msg["method"] == "Runtime.evaluate":
                await ws.send(json.dumps({"id": msg["id"], "result": {
                    "exceptionDetails": {"text": "boom"}}}))
                held.clear()
            elif msg["method"] == "Err.out":
                await ws.send(json.dumps({"id": msg["id"],
                                          "error": {"message": "No node"}}))
                held.clear()
            elif len(held) == 2:
                for m in reversed(held):
                    await ws.send(json.dumps({"method": "Page.x", "params": {}}))
                    await ws.send(json.dumps(
                        {"id": m["id"], "result": {"echo": m["method"]}}))
                held.clear()

    server, url = await _serve(handler)
    yield url
    server.close()
    await server.wait_closed()


@pytest.fixture
async def fake_cdp_silent():
    async def handler(ws):
        async for _ in ws:
            pass

    server, url = await _serve(handler)
    yield url
    server.close()
    await server.wait_closed()


@pytest.fixture
async def fake_cdp_closes():
    async def handler(ws):
        await ws.recv()
        await ws.close()

    server, url = await _serve(handler)
    yield url
    server.close()
    await server.wait_closed()


@pytest.fixture
async def fake_cdp_records_headers():
    seen = {}

    async def handler(ws):
        seen.update({k: v for k, v in ws.request.headers.items()})
        await ws.wait_closed()

    server, url = await _serve(handler)
    yield url, seen
    server.close()
    await server.wait_closed()


async def test_send_correlates_ids_and_ignores_events(fake_cdp):
    c = await CdpClient.connect(fake_cdp, 5)
    try:
        r1, r2 = await asyncio.gather(c.send("A.one"), c.send("B.two"))
    finally:
        await c.close()
    assert r1 == {"echo": "A.one"} and r2 == {"echo": "B.two"}


async def test_send_error_reply_raises_protocol(fake_cdp):
    c = await CdpClient.connect(fake_cdp, 5)
    try:
        with pytest.raises(CdpError) as e:
            await c.send("Err.out")
    finally:
        await c.close()
    assert e.value.code == "CDP_PROTOCOL" and "No node" in e.value.message


async def test_send_timeout(fake_cdp_silent):
    c = await CdpClient.connect(fake_cdp_silent, 5)
    try:
        with pytest.raises(CdpError) as e:
            await c.send("X.y", timeout_s=0.2)
    finally:
        await c.close()
    assert e.value.code == "CDP_TIMEOUT"


async def test_server_close_fails_pending_with_closed(fake_cdp_closes):
    c = await CdpClient.connect(fake_cdp_closes, 5)
    with pytest.raises(CdpError) as e:
        await c.send("X.y")
    assert e.value.code == "CDP_CLOSED" and c.closed
    await c.close()


async def test_evaluate_exception_raises_js_error(fake_cdp):
    c = await CdpClient.connect(fake_cdp, 5)
    try:
        with pytest.raises(CdpError) as e:
            await c.evaluate("throw 1")
    finally:
        await c.close()
    assert e.value.code == "JS_ERROR" and "boom" in e.value.message


async def test_connect_sends_no_origin_header(fake_cdp_records_headers):
    url, seen = fake_cdp_records_headers
    c = await CdpClient.connect(url, 5)
    await c.close()
    assert seen and "origin" not in {k.lower() for k in seen}
