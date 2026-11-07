import asyncio
import statistics
import time

from agent.browser.actions import (
    ClickStep,
    FillStep,
    PressStep,
    ScrollStep,
    SelectStep,
    WaitForTextStep,
    perform,
)
from agent.browser.cdp import CdpError
from agent.browser.snapshot import INDEX_VERSION, take_snapshot

HOSTS = ["127.0.0.1"]


async def test_snapshot_lists_only_visible_safe_controls(cdp_page):
    c = await cdp_page("flow_like.html")
    s = await take_snapshot(c)
    labels = {a.label for a in s.actions}
    assert {"Prompt box", "Start generation", "Aspect ratio"} <= labels
    assert not labels & {"Hidden", "Ghost", "Disabled", "Below fold", "Password", "Upload"}
    assert s.can_scroll_down and not s.can_scroll_up
    assert s.title == "Flow-like fixture"
    assert s.url.endswith("/flow_like.html")
    assert "old text" in s.text and "Below fold" not in s.text
    assert s.omitted == 0


async def test_ids_are_stable_across_snapshots(cdp_page):
    c = await cdp_page("flow_like.html")
    a = await take_snapshot(c)
    b = await take_snapshot(c)
    assert {x.label: x.id for x in a.actions} == {x.label: x.id for x in b.actions}
    assert all(x.id.startswith("e") and x.id[1:].isdigit() for x in a.actions)


async def test_prompt_box_is_fill_with_value_and_select_has_options(cdp_page):
    c = await cdp_page("flow_like.html")
    s = await take_snapshot(c)
    by_label = {a.label: a for a in s.actions}
    prompt, aspect = by_label["Prompt box"], by_label["Aspect ratio"]
    assert prompt.kind == "fill" and prompt.role == "textbox" and prompt.value == "old text"
    assert aspect.kind == "select" and aspect.role == "combobox"
    assert aspect.value == "Portrait"
    assert aspect.options == ["Portrait", "Landscape", "Square"]
    start = by_label["Start generation"]
    assert start.kind == "click" and start.role == "button" and start.options is None


async def test_snapshot_is_fast(cdp_page):
    c = await cdp_page("flow_like.html")
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        await take_snapshot(c)
        times.append(time.perf_counter() - t0)
    assert statistics.median(times) < 0.25


async def test_resolve_and_select_option_contract(cdp_page):
    import json

    from agent.browser.snapshot import SNAPSHOT_JS, index_expr

    c = await cdp_page("flow_like.html")
    by_label = {a.label: a for a in (await take_snapshot(c)).actions}
    n = {label: int(a.id[1:]) for label, a in by_label.items()}

    ok = await c.evaluate(index_expr(f"resolve({n['Start generation']})"))
    assert ok["ok"] and ok["kind"] == "click" and 0 < ok["x"] < 1200 and 0 < ok["y"] < 800
    assert (await c.evaluate(index_expr(f"resolve({n['Prompt box']})")))["kind"] == "fill"

    bad = await c.evaluate(index_expr(f"selectOption({n['Aspect ratio']}, 'Nope')"))
    assert bad == {"ok": False, "reason": "no_such_option"}
    wrong = await c.evaluate(index_expr(f"selectOption({n['Start generation']}, 'Square')"))
    assert wrong == {"ok": False, "reason": "wrong_kind"}
    picked = await c.evaluate(
        index_expr(f"selectOption({n['Aspect ratio']}, {json.dumps('Landscape')})"))
    assert picked == {"ok": True}
    assert "aspect:Landscape" in await c.evaluate("document.body.innerText")
    assert "aspect:Landscape" in await c.evaluate(index_expr("visibleText()"))

    # Re-injecting the same version keeps the index (and so the ids).
    again = await c.evaluate(SNAPSHOT_JS + ";" + index_expr(f"resolve({n['Start generation']})"))
    assert again["ok"]

    await c.evaluate("fixture.remove()")
    assert await c.evaluate(index_expr(f"resolve({n['Remove me']})")) == {
        "ok": False, "reason": "stale"}
    await c.evaluate("fixture.cover()")
    assert await c.evaluate(index_expr(f"resolve({n['Start generation']})")) == {
        "ok": False, "reason": "covered"}

    # A different version on the page is replaced, and the old ids no longer resolve.
    await c.evaluate("window.__fkIndex = {version: 0}")
    assert await c.evaluate(index_expr("resolve(1)")) == {"ok": False, "reason": "stale"}
    await c.evaluate(SNAPSHOT_JS)
    assert await c.evaluate("window.__fkIndex.version") == INDEX_VERSION


async def _ids(c):
    return {a.label: a.id for a in (await take_snapshot(c)).actions}


async def _body(c):
    return await c.evaluate("document.body.innerText")


async def test_click_dispatches_real_click(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    r = await perform(c, ClickStep(op="click", target=ids["Start generation"]), HOSTS)
    assert r.ok and r.op == "click" and r.error is None
    assert "clicked:start" in await _body(c)


async def test_fill_contenteditable_replaces_text_with_thai_and_emoji(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    text = "แมวน่ารักนั่งบนโต๊ะ 🐱\nline two"
    r = await perform(c, FillStep(op="fill", target=ids["Prompt box"], text=text), HOSTS)
    assert r.ok
    paragraphs = (
        "[...document.querySelectorAll('.ProseMirror p')]"
        ".map(p => p.textContent).join('\\n')"
    )
    assert await c.evaluate(paragraphs) == text
    assert "old text" not in await c.evaluate(
        "document.querySelector('.ProseMirror').textContent")


async def test_fill_with_empty_text_clears_editor(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    r = await perform(c, FillStep(op="fill", target=ids["Prompt box"], text=""), HOSTS)
    assert r.ok
    assert await c.evaluate(
        "document.querySelector('.ProseMirror').textContent") == ""
    assert "key:Backspace" in await _body(c)


async def test_fill_on_button_is_wrong_kind(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    r = await perform(c, FillStep(op="fill", target=ids["Start generation"], text="x"), HOSTS)
    assert not r.ok and r.error == "WRONG_KIND"
    assert "clicked:start" not in await _body(c)


async def test_select_option_fires_change(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    aspect = ids["Aspect ratio"]
    r = await perform(c, SelectStep(op="select", target=aspect, option="Landscape"), HOSTS)
    assert r.ok and "aspect:Landscape" in await _body(c)
    r2 = await perform(c, SelectStep(op="select", target=aspect, option="Nope"), HOSTS)
    assert r2.error == "NO_SUCH_OPTION"
    r3 = await perform(
        c, SelectStep(op="select", target=ids["Start generation"], option="Square"), HOSTS)
    assert r3.error == "WRONG_KIND"


async def test_select_on_hidden_or_disabled_target_fails_without_change(cdp_page):
    c = await cdp_page("flow_like.html")
    aspect = (await _ids(c))["Aspect ratio"]
    step = SelectStep(op="select", target=aspect, option="Landscape")

    await c.evaluate("document.getElementById('aspect').style.display = 'none'")
    r = await perform(c, step, HOSTS)
    assert (r.ok, r.error, r.detail) == (False, "STALE_ELEMENT", "hidden")

    await c.evaluate("{ const a = document.getElementById('aspect');"
                     " a.style.display = ''; a.disabled = true; }")
    assert (await perform(c, step, HOSTS)).error == "STALE_ELEMENT"

    await c.evaluate("{ const a = document.getElementById('aspect'); a.disabled = false;"
                     " a.options[1].disabled = true; }")
    assert (await perform(c, step, HOSTS)).error == "STALE_ELEMENT"

    assert "aspect:" not in await _body(c)
    assert await c.evaluate("document.getElementById('aspect').value") == "Portrait"


async def test_click_removed_element_is_stale(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    await c.evaluate("fixture.remove()")
    r = await perform(c, ClickStep(op="click", target=ids["Remove me"]), HOSTS)
    assert r.error == "STALE_ELEMENT"


async def test_unknown_id_is_stale_even_before_any_snapshot(cdp_page):
    c = await cdp_page("flow_like.html")
    r = await perform(c, ClickStep(op="click", target="e999"), HOSTS)
    assert r.error == "STALE_ELEMENT"
    assert await c.evaluate("window.__fkIndex.version") == INDEX_VERSION


async def test_click_covered_element_is_obscured(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    await c.evaluate("fixture.cover()")
    r = await perform(c, ClickStep(op="click", target=ids["Start generation"]), HOSTS)
    assert r.error == "OBSCURED" and "clicked:start" not in await _body(c)


async def test_scroll_reveals_below_fold(cdp_page):
    c = await cdp_page("flow_like.html")
    assert "Below fold" not in await _ids(c)
    r = await perform(c, ScrollStep(op="scroll", dy=2000), HOSTS)
    assert r.ok
    assert "Below fold" in {a.label for a in (await take_snapshot(c)).actions}


async def test_press_enter_reaches_focused_editor(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    assert (await perform(c, ClickStep(op="click", target=ids["Prompt box"]), HOSTS)).ok
    r = await perform(c, PressStep(op="press", key="Enter"), HOSTS)
    assert r.ok
    assert "key:Enter" in await _body(c)


async def test_wait_for_text_present_and_absent(cdp_page):
    c = await cdp_page("flow_like.html")
    await c.evaluate("fixture.showLater(1500, 'Generation complete')")
    r = await perform(
        c, WaitForTextStep(op="wait_for_text", text="Generation complete", timeout_s=5), HOSTS)
    assert r.ok
    gone = await perform(
        c, WaitForTextStep(op="wait_for_text", text="never", timeout_s=1.5, absent=True), HOSTS)
    assert gone.ok
    t0 = time.monotonic()
    r2 = await perform(
        c, WaitForTextStep(op="wait_for_text", text="never", timeout_s=1.5), HOSTS)
    assert r2.error == "WAIT_TIMEOUT"
    assert 1.4 < time.monotonic() - t0 < 3.0


async def test_steps_refused_after_leaving_allowed_host(cdp_page):
    c = await cdp_page("flow_like.html")
    ids = await _ids(c)
    await perform(c, ClickStep(op="click", target=ids["Go elsewhere"]), HOSTS)
    deadline = time.monotonic() + 3
    host = None
    while time.monotonic() < deadline:
        try:
            host = await c.evaluate("location.host")
        except CdpError:
            host = None
        if host and host.startswith("localhost:"):
            break
        await asyncio.sleep(0.05)
    assert host and host.startswith("localhost:")
    r = await perform(c, ClickStep(op="click", target="e1"), HOSTS)
    assert not r.ok and r.error == "HOST_NOT_ALLOWED"
    # Refused before re-injecting: the off-host page never gets an index.
    assert await c.evaluate("typeof window.__fkIndex") == "undefined"


async def test_inner_scroll_container_shows_scroll_hints(cdp_page):
    c = await cdp_page("app_shell.html")
    s = await take_snapshot(c)
    labels = {a.label for a in s.actions}
    assert "Top button" in labels and "Deep button" not in labels
    assert s.can_scroll_down and not s.can_scroll_up
    r = await perform(c, ScrollStep(op="scroll", dy=2000), HOSTS)
    assert r.ok
    deadline = time.monotonic() + 3
    while True:
        s2 = await take_snapshot(c)
        if s2.can_scroll_up or time.monotonic() > deadline:
            break
        await asyncio.sleep(0.05)
    assert s2.can_scroll_up and not s2.can_scroll_down
    assert "Deep button" in {a.label for a in s2.actions}
    # The document itself never scrolled: the hint came from the pane.
    assert await c.evaluate("document.scrollingElement.scrollTop") == 0

async def test_wait_for_text_stops_when_page_leaves_allowed_host(cdp_page):
    c = await cdp_page("flow_like.html")
    await c.evaluate("setTimeout(() => document.querySelector('#away').click(), 800)")
    t0 = time.monotonic()
    r = await perform(
        c, WaitForTextStep(op="wait_for_text", text="never appears", timeout_s=5), HOSTS)
    assert (r.ok, r.error) == (False, "HOST_NOT_ALLOWED")
    assert r.detail and "localhost:" in r.detail
    assert time.monotonic() - t0 < 4
