import statistics
import time

from agent.browser.snapshot import take_snapshot


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
    assert await c.evaluate("window.__fkIndex.version") == 1
