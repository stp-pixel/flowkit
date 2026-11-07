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
