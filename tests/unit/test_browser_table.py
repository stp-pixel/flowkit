from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from agent.browser import actions
from agent.browser.actions import FillStep, PressStep, Step, perform
from agent.browser.cdp import CdpError
from agent.browser.snapshot import (
    INDEX_VERSION,
    SNAPSHOT_JS,
    Action,
    Snapshot,
    index_expr,
    parse_snapshot,
    render_table,
)


def test_render_table_exact_format():
    snap = Snapshot(
        url="https://flow.google.com/p",
        title="Flow",
        text="Hello world",
        actions=[
            Action("e3", "click", "button", "Start generation"),
            Action("e4", "fill", "textbox", "Prompt box", "a cat"),
            Action(
                "e9",
                "select",
                "combobox",
                "Aspect ratio",
                "Portrait",
                ["Portrait", "Landscape", "Square"],
            ),
        ],
        omitted=12,
        can_scroll_up=False,
        can_scroll_down=True,
    )
    assert render_table(snap, text_chars=5) == (
        "url: https://flow.google.com/p\ntitle: Flow\n"
        "[e3] click button : Start generation\n"
        '[e4] fill textbox : Prompt box = "a cat"\n'
        '[e9] select combobox : Aspect ratio = "Portrait" {Portrait|Landscape|Square}\n'
        "… 12 more actions not shown\nscroll: down\n--- text ---\nHello"
    )


def test_render_table_omits_empty_sections():
    snap = Snapshot(
        url="https://flow.google.com/p",
        title="Flow",
        text="Hello world",
        actions=[Action("e1", "fill", "textbox", "Prompt box", "")],
        omitted=0,
        can_scroll_up=False,
        can_scroll_down=False,
    )
    t = render_table(snap, text_chars=0)
    assert "…" not in t and "scroll:" not in t and "--- text ---" not in t and ' = ""' not in t
    assert t == "url: https://flow.google.com/p\ntitle: Flow\n[e1] fill textbox : Prompt box"


def test_render_table_scroll_both_ways_and_empty_text():
    snap = Snapshot(
        url="u", title="t", text="", actions=[], omitted=0,
        can_scroll_up=True, can_scroll_down=True,
    )
    assert render_table(snap) == "url: u\ntitle: t\nscroll: up down"


def test_parse_snapshot_round_trip():
    raw = {
        "url": "http://127.0.0.1:1234/flow_like.html",
        "title": "Flow-like fixture",
        "text": "Flow-like fixture\nold text",
        "actions": [
            {"id": "e1", "kind": "fill", "role": "textbox", "label": "Prompt box",
             "value": "old text", "options": None},
            {"id": "e2", "kind": "click", "role": "button", "label": "Start generation",
             "value": None, "options": None},
            {"id": "e7", "kind": "select", "role": "combobox", "label": "Aspect ratio",
             "value": "Portrait", "options": ["Portrait", "Landscape", "Square"]},
        ],
        "omitted": 3,
        "can_scroll_up": False,
        "can_scroll_down": True,
    }
    snap = parse_snapshot(raw)
    assert snap == Snapshot(
        url="http://127.0.0.1:1234/flow_like.html",
        title="Flow-like fixture",
        text="Flow-like fixture\nold text",
        actions=[
            Action("e1", "fill", "textbox", "Prompt box", "old text", None),
            Action("e2", "click", "button", "Start generation", None, None),
            Action("e7", "select", "combobox", "Aspect ratio", "Portrait",
                   ["Portrait", "Landscape", "Square"]),
        ],
        omitted=3,
        can_scroll_up=False,
        can_scroll_down=True,
    )


def test_js_version_matches_python():
    assert f"version: {INDEX_VERSION}" in SNAPSHOT_JS or f"VERSION = {INDEX_VERSION}" in SNAPSHOT_JS


def test_js_is_read_only():
    for banned in ("fetch(", "XMLHttpRequest", "sendBeacon", "captureScreenshot"):
        assert banned not in SNAPSHOT_JS
    assert SNAPSHOT_JS.startswith(
        "// Adapted from browser-use/jev-ultrafast (MIT). "
        "https://github.com/browser-use/jev-ultrafast"
    )


def test_index_expr_guards_on_version():
    assert index_expr("resolve(3)") == (
        f"(window.__fkIndex && window.__fkIndex.version === {INDEX_VERSION}) "
        "? window.__fkIndex.resolve(3) : {ok:false, reason:'stale'}"
    )


def test_step_validation():
    adapter = TypeAdapter(Step)
    good = [
        {"op": "click", "target": "e12"},
        {"op": "fill", "target": "e1", "text": ""},
        {"op": "fill", "target": "e1", "text": "x" * 4000},
        {"op": "select", "target": "e7", "option": "Landscape"},
        {"op": "press", "key": "Enter"},
        {"op": "scroll", "dy": -2000},
        {"op": "wait", "ms": 0},
        {"op": "wait_for_text", "text": "x", "timeout_s": 600, "absent": True},
    ]
    for ok in good:
        assert adapter.validate_python(ok).op == ok["op"]
    assert adapter.validate_python({"op": "wait_for_text", "text": "x"}).timeout_s == 60
    bad = [
        {"op": "click", "target": "12"},
        {"op": "click", "target": "e12 "},
        {"op": "fill", "target": "e1", "text": "x" * 4001},
        {"op": "scroll", "dy": 5000},
        {"op": "wait", "ms": 20000},
        {"op": "wait", "ms": -1},
        {"op": "press", "key": "F5"},
        {"op": "wait_for_text", "text": "x", "timeout_s": 601},
        {"op": "wait_for_text", "text": "x", "timeout_s": 0},
        {"op": "wait_for_text", "text": ""},
        {"op": "eval", "code": "1"},
    ]
    for b in bad:
        with pytest.raises(ValidationError):
            adapter.validate_python(b)


def test_actions_module_never_screenshots():
    assert "captureScreenshot" not in Path(actions.__file__).read_text()


class _FakeClient:
    """Records CDP calls; the page is on 127.0.0.1 and every id resolves as a fill."""

    def __init__(self, fail_with: CdpError | None = None):
        self.fail_with = fail_with
        self.sent: list[tuple[str, dict]] = []
        self.evaluated: list[str] = []

    async def evaluate(self, expression):
        if self.fail_with:
            raise self.fail_with
        self.evaluated.append(expression)
        if expression == "location.href":
            return "http://127.0.0.1:9/p"
        return {"ok": True, "x": 10, "y": 20, "kind": "fill"}

    async def send(self, method, params=None, timeout_s=None):
        self.sent.append((method, params))
        return {}


async def test_fill_dispatches_pinned_input_events(monkeypatch):
    monkeypatch.setattr(actions.sys, "platform", "linux")
    c = _FakeClient()
    r = await perform(c, FillStep(op="fill", target="e4", text="hi"), ["127.0.0.1"])
    assert r == actions.StepResult("fill", True)
    assert c.evaluated[1] == SNAPSHOT_JS + "\n;" + index_expr("resolve(4)")
    key = {"key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2}
    assert c.sent == [
        ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 10, "y": 20}),
        ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 10, "y": 20,
                                      "button": "left", "buttons": 1, "clickCount": 1}),
        ("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 10, "y": 20,
                                      "button": "left", "buttons": 0, "clickCount": 1}),
        ("Input.dispatchKeyEvent", {"type": "keyDown", **key, "commands": ["selectAll"]}),
        ("Input.dispatchKeyEvent", {"type": "keyUp", **key}),
        ("Input.insertText", {"text": "hi"}),
    ]


async def test_press_sends_text_only_for_enter():
    c = _FakeClient()
    await perform(c, PressStep(op="press", key="Enter"), ["127.0.0.1"])
    await perform(c, PressStep(op="press", key="Tab"), ["127.0.0.1"])
    assert [p for _, p in c.sent] == [
        {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
         "text": "\r"},
        {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
        {"type": "keyDown", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
        {"type": "keyUp", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    ]


async def test_cdp_closed_becomes_a_result_and_other_errors_propagate():
    closed = _FakeClient(CdpError("CDP_CLOSED", "gone"))
    r = await perform(closed, PressStep(op="press", key="Enter"), ["127.0.0.1"])
    assert (r.op, r.ok, r.error) == ("press", False, "CDP_CLOSED")
    with pytest.raises(CdpError) as exc:
        await perform(_FakeClient(CdpError("CDP_TIMEOUT", "slow")),
                      PressStep(op="press", key="Enter"), ["127.0.0.1"])
    assert exc.value.code == "CDP_TIMEOUT"
