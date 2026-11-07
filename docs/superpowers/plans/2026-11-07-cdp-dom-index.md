# DOM-Indexed Browser Control (CDP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Claude session drive the real Google Flow UI through CDP, using an indexed table of visible controls instead of screenshots. This also unlocks the Flow features the batch RPC layer can't reach.

**Architecture:** A new `agent/browser/` package talks to the user's browser over CDP at `127.0.0.1:9333` using the existing `websockets` and `httpx` dependencies. An injected `snapshot.js` (adapted from jev-ultrafast, MIT) indexes the visible controls. Python renders them as `[eN] kind role : label` lines and executes steps with native `Input.*` events. A gated FastAPI router (`/api/browser/*`) exposes snapshot and batched act, and a `/fk-browser` skill teaches the loop.

**Tech Stack:** Python 3.12+, FastAPI, `websockets>=12`, `httpx>=0.25`, vanilla JS, pytest (`asyncio_mode=auto`), Chromium for the integration tests.

**Spec:** `docs/superpowers/specs/2026-11-07-cdp-dom-index-design.md`

## Global Constraints

- CDP is loopback only: always `127.0.0.1`. There is no host setting.
- `FLOW_CDP_PORT` defaults to `9333`. It must never default to `9222`, which is `WS_PORT`, the extension bridge.
- `FLOW_CDP_ENABLED` defaults to off (`"0"`). While it is off, every `/api/browser/*` route returns 404 with detail `browser control disabled; set FLOW_CDP_ENABLED=1`.
- `FLOW_CDP_ALLOWED_HOSTS` defaults to `flow.google.com` (comma-separated, exact hostname match).
- `FLOW_CDP_TIMEOUT_S` defaults to `10`.
- `agent/browser/` never contains the string `captureScreenshot`. Injected JS never calls `fetch`, `XMLHttpRequest` or `sendBeacon`.
- No new entries in `requirements.txt`.
- Docs and skills never recommend `--remote-allow-origins=*`. The Python client sends no `Origin` header, so Chrome doesn't need it, and `*` lets any web page take over the signed-in browser.
- Docs tell users to launch with a **dedicated** `--user-data-dir`. Chrome 136+ ignores `--remote-debugging-port` on the default profile.
- `httpx` clients in `agent/browser/` use `trust_env=False`, so `HTTPS_PROXY` never captures `127.0.0.1` traffic.
- Skills are written in English. `scripts/validate_skills.py` must pass.
- `snapshot.js` starts with the attribution line `// Adapted from browser-use/jev-ultrafast (MIT). https://github.com/browser-use/jev-ultrafast`.
- CDP error codes are exactly: `CDP_UNREACHABLE`, `CDP_TIMEOUT`, `CDP_CLOSED`, `CDP_PROTOCOL`, `JS_ERROR`, `NO_FLOW_PAGE`, `PORT_CONFLICT`. Step error codes are exactly: `STALE_ELEMENT`, `OBSCURED`, `WRONG_KIND`, `NO_SUCH_OPTION`, `WAIT_TIMEOUT`, `HOST_NOT_ALLOWED`, `CDP_CLOSED`.

## Review Focus

1. **The connection drops in the middle of a step** (the browser is restarted, or the tab is closed after the click went out). The step must report `CDP_CLOSED` and must **not** be replayed on a new connection, because a replayed Generate click spends Veo credits twice. The *next* call should reconnect by itself. → Task 4, `test_act_does_not_replay_step_after_disconnect` and `test_next_call_reconnects_after_disconnect`.
2. **The page re-renders between snapshot and act.** Flow swaps nodes after typing, and toasts and dialogs cover buttons. A stale target gives `STALE_ELEMENT` and a covered one gives `OBSCURED`, with no mouse event dispatched. → Task 3, `test_click_removed_element_is_stale`, `test_click_covered_element_is_obscured`.
3. **Thai, emoji and multi-line prompts in Flow's ProseMirror box.** `fill` must reproduce the text exactly, replacing whatever was in the box before. → Task 3, `test_fill_contenteditable_replaces_text_with_thai_and_emoji`.
4. **A click navigates away from Flow,** for example to the `accounts.google.com` re-auth page. Later steps fail with `HOST_NOT_ALLOWED`, and the snapshot shows the URL only, with no text or actions from that page. → Task 3, `test_steps_refused_after_leaving_allowed_host`; Task 4, `test_snapshot_on_disallowed_host_reveals_url_only`.
5. **The user follows the original post and launches the browser on 9222,** or sets `FLOW_CDP_PORT=9222`. Status must say `PORT_CONFLICT` or `CDP_UNREACHABLE`, and the hint must name `--remote-debugging-port=9333`, rather than throwing a 500. → Task 1, `test_list_pages_unreachable_hint_names_port`; Task 4, `test_port_equal_to_ws_port_is_conflict`.

---

## File Structure

| File | Responsibility |
|---|---|
| `agent/config.py` (modify) | four `FLOW_CDP_*` settings |
| `agent/browser/__init__.py` | empty package marker |
| `agent/browser/cdp.py` | target discovery, page selection, CDP JSON-RPC client |
| `agent/browser/snapshot.js` | in-page index: `snapshot()`, `resolve()`, `selectOption()`, `visibleText()` |
| `agent/browser/snapshot.py` | load the JS, parse the result, render the table |
| `agent/browser/actions.py` | step models plus `perform()` (native input dispatch) |
| `agent/browser/session.py` | singleton: lock, lazy connect and reconnect, host gate, port check |
| `agent/api/browser.py` | `/api/browser/{status,snapshot,act}` |
| `agent/main.py` (modify) | include the router |
| `tests/unit/test_browser_cdp.py`, `test_browser_table.py`, `test_browser_session.py`, `test_browser_api.py` | unit tests (run in CI) |
| `tests/integration/__init__.py`, `tests/integration/conftest.py`, `tests/integration/test_browser_live.py` | real Chromium tests (skip when no browser) |
| `tests/fixtures/browser/flow_like.html`, `tests/fixtures/browser/other.html` | fixture pages |
| `skills/fk-browser.md` + synced copies | the agent loop |
| `docs/BROWSER_CDP.md`, `CLAUDE.md`, `README.md`, `skills/README.md`, `skills/fk-doctor.md` (modify) | setup and error taxonomy |

---

### Task 1: Config + CDP client

**Files:**
- Modify: `agent/config.py` (new section after `# ─── Flow batchexecute`)
- Create: `agent/browser/__init__.py`, `agent/browser/cdp.py`
- Test: `tests/unit/test_browser_cdp.py`

**Interfaces:**
- Produces:
  - `config.FLOW_CDP_ENABLED: bool`, `FLOW_CDP_PORT: int`, `FLOW_CDP_ALLOWED_HOSTS: list[str]`, `FLOW_CDP_TIMEOUT_S: float`
  - `class CdpError(RuntimeError)` with `.code: str`, `.message: str`, `.hint: str = ""`, built as `CdpError(code, message, hint="")`
  - `@dataclass(frozen=True) class PageTarget: id: str; url: str; title: str; ws_url: str`
  - `async def list_pages(port: int, *, transport: httpx.AsyncBaseTransport | None = None) -> list[PageTarget]`
  - `def pick_page(pages: list[PageTarget], allowed_hosts: list[str], prefer_substring: str = "") -> PageTarget`
  - `def host_allowed(url: str, allowed_hosts: list[str]) -> bool`
  - `class CdpClient`: `@classmethod async connect(ws_url: str, timeout_s: float) -> CdpClient`; `async send(method: str, params: dict | None = None, timeout_s: float | None = None) -> dict`; `async evaluate(expression: str) -> Any`; `closed: bool` (property); `async close() -> None`

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_browser_cdp.py`

```python
def test_config_defaults(monkeypatch):            # reload agent.config with env cleared
    assert config.FLOW_CDP_ENABLED is False
    assert config.FLOW_CDP_PORT == 9333 != config.WS_PORT
    assert config.FLOW_CDP_ALLOWED_HOSTS == ["flow.google.com"]
    assert config.FLOW_CDP_TIMEOUT_S == 10.0

async def test_list_pages_keeps_only_page_targets():   # httpx.MockTransport serving /json/list
    # body: [{"type":"page","id":"A","url":"https://flow.google.com/p/1","title":"Flow","webSocketDebuggerUrl":"ws://x/A"},
    #        {"type":"service_worker",...}, {"type":"iframe",...}]
    assert [p.id for p in pages] == ["A"]

async def test_list_pages_unreachable_hint_names_port():   # transport raises httpx.ConnectError
    with pytest.raises(CdpError) as e: await list_pages(9333, transport=t)
    assert e.value.code == "CDP_UNREACHABLE"
    assert "--remote-debugging-port=9333" in e.value.hint
    assert "--user-data-dir" in e.value.hint

def test_pick_page_filters_host_and_prefers_project():
    pages = [P("https://accounts.google.com/x"), P("https://flow.google.com/project/aaa"),
             P("https://flow.google.com/project/bbb"), P("https://flow.google.com.evil.example/")]
    assert pick_page(pages, ["flow.google.com"], "bbb").url.endswith("/bbb")
    assert pick_page(pages, ["flow.google.com"]).url.endswith("/aaa")   # first allowed

def test_pick_page_none_allowed_raises_no_flow_page():
    with pytest.raises(CdpError) as e: pick_page([P("https://example.com/")], ["flow.google.com"])
    assert e.value.code == "NO_FLOW_PAGE"

def test_host_allowed_is_exact_hostname():
    assert host_allowed("https://flow.google.com/x", ["flow.google.com"])
    assert not host_allowed("https://flow.google.com.evil.example/", ["flow.google.com"])
    assert not host_allowed("about:blank", ["flow.google.com"])

async def test_send_correlates_ids_and_ignores_events(fake_cdp):
    # fake_cdp: websockets.serve on 127.0.0.1:0. It replies to each {"id":n,...} with an
    # unrelated event {"method":"Page.x"} first, then {"id":n,"result":{"echo":method}}, out of order for two concurrent sends.
    r1, r2 = await asyncio.gather(c.send("A.one"), c.send("B.two"))
    assert r1 == {"echo": "A.one"} and r2 == {"echo": "B.two"}

async def test_send_error_reply_raises_protocol(fake_cdp):   # reply {"id":n,"error":{"message":"No node"}}
    assert exc.code == "CDP_PROTOCOL" and "No node" in exc.message

async def test_send_timeout(fake_cdp_silent):
    with pytest.raises(CdpError) as e: await c.send("X.y", timeout_s=0.2)
    assert e.value.code == "CDP_TIMEOUT"

async def test_server_close_fails_pending_with_closed(fake_cdp_closes):
    assert e.value.code == "CDP_CLOSED" and c.closed

async def test_evaluate_exception_raises_js_error(fake_cdp):  # result has exceptionDetails.text "boom"
    assert e.value.code == "JS_ERROR" and "boom" in e.value.message

async def test_connect_sends_no_origin_header(fake_cdp_records_headers):
    assert "origin" not in {k.lower() for k in headers}
```

- [ ] **Step 2: Run** `python -m pytest tests/unit/test_browser_cdp.py -q`. Expected: FAIL (`ModuleNotFoundError: agent.browser`).

- [ ] **Step 3: Implement.**
  - Config: parse the env vars as in the Global Constraints, using the same style as the neighbouring settings.
  - `list_pages`: `GET http://127.0.0.1:{port}/json/list` through `httpx.AsyncClient(timeout=3, trust_env=False, transport=transport)`. Keep only entries with `type == "page"`. Map `httpx.ConnectError` and `httpx.TimeoutException` to `CdpError("CDP_UNREACHABLE", ..., hint=f"Start the browser with --remote-debugging-port={port} --user-data-dir=<dedicated profile dir>; see docs/BROWSER_CDP.md")`.
  - `host_allowed`: `urlsplit(url).hostname in allowed_hosts`, with `False` for a `None` hostname.
  - `CdpClient.connect`: `websockets.connect(ws_url, max_size=16 * 2**20, open_timeout=timeout_s)`, with no `origin` argument. Start a reader task that resolves `self._pending[id]` futures and drops messages that carry no `id`. When the reader ends, fail every pending future with `CdpError("CDP_CLOSED", ...)` and set `closed`.
  - `evaluate`: `Runtime.evaluate` with `returnByValue=True, awaitPromise=True`. Return `result["result"].get("value")`. If `exceptionDetails` is present, raise `JS_ERROR` with `exceptionDetails["exception"]["description"]`, falling back to `["text"]`.

- [ ] **Step 4: Run** `python -m pytest tests/unit/test_browser_cdp.py -q`. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/config.py agent/browser/__init__.py agent/browser/cdp.py tests/unit/test_browser_cdp.py
git commit -m "feat(browser): CDP client, page discovery and FLOW_CDP_* config"
```

---

### Task 2: Snapshot JS, parser and interaction table

**Files:**
- Create: `agent/browser/snapshot.js`, `agent/browser/snapshot.py`
- Create: `tests/fixtures/browser/flow_like.html`, `tests/fixtures/browser/other.html`
- Create: `tests/integration/__init__.py`, `tests/integration/conftest.py`, `tests/integration/test_browser_live.py`
- Test: `tests/unit/test_browser_table.py`

**Interfaces:**
- Consumes: `CdpClient.evaluate` (Task 1).
- Produces:
  - JS global `window.__fkIndex = { version: 1, snapshot(), resolve(n), selectOption(n, label), visibleText() }`. The script is idempotent: it keeps an existing index when `version` matches and replaces it otherwise.
    - `snapshot()` returns `{url, title, text, actions: [{id: "e<n>", kind: "click"|"fill"|"select", role, label, value, options}], omitted, can_scroll_up, can_scroll_down}`
    - `resolve(n)` returns `{ok: true, x, y, kind} | {ok: false, reason: "stale"|"hidden"|"offscreen"|"covered"}`
    - `selectOption(n, label)` returns `{ok: true} | {ok: false, reason: "stale"|"no_such_option"|"wrong_kind"}`
    - `visibleText()` returns a string
  - `snapshot.py`:
    - `SNAPSHOT_JS: str`, `INDEX_VERSION = 1`
    - `@dataclass Action: id: str; kind: str; role: str; label: str; value: str | None = None; options: list[str] | None = None`
    - `@dataclass Snapshot: url: str; title: str; text: str; actions: list[Action]; omitted: int; can_scroll_up: bool; can_scroll_down: bool`
    - `def parse_snapshot(raw: dict) -> Snapshot`
    - `async def take_snapshot(client: CdpClient) -> Snapshot`, which evaluates `SNAPSHOT_JS + "\n;window.__fkIndex.snapshot()"`
    - `def render_table(snap: Snapshot, text_chars: int = 600) -> str`
    - `def index_expr(call: str) -> str`, which returns `f"(window.__fkIndex && window.__fkIndex.version === {INDEX_VERSION}) ? window.__fkIndex.{call} : {{ok:false, reason:'stale'}}"` (used by Task 3)
  - Integration fixture `cdp_page(page_name: str) -> CdpClient` in `tests/integration/conftest.py`. It serves `tests/fixtures/browser/` over `http.server` on `127.0.0.1:0`, launches Chromium, and returns a client attached to that page.

**JS rules.** These are values taken from jev-ultrafast. They aren't derivable from the tests, so copy them.
- Candidate selector: `a[href],button,input,textarea,select,summary,[contenteditable="true"],[contenteditable=""]`, plus `[role=X]` for X in `button link checkbox radio switch tab menuitem menuitemradio option gridcell combobox textbox searchbox spinbutton`.
- Skip an element when any of these holds:
  - its `type` is one of `password`, `file` or `hidden`
  - `e.closest('[aria-hidden="true"],[inert]')` matches
  - `!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})`
  - its rect centre is outside `0..innerWidth` × `0..innerHeight`
  - it is `disabled` or has `aria-disabled="true"`
- Ids are stable: `e<n>`, where `n` comes from a `WeakMap` counter, and `nodes: Map<n, WeakRef<Element>>`. This is a deliberate deviation from jev's positional ids: an id stays valid for as long as its node lives.
- `kind`:
  - `select` for `<select>`
  - `fill` for `textarea`, text-like `input` (`text search email url tel number` or no type), contenteditable, or `role` in `textbox searchbox spinbutton`
  - `click` for everything else
- `role`: the explicit `role`, else the implicit one (`button link textbox combobox checkbox radio`), else the lowercase tag name.
- `label`: the first non-empty of `aria-labelledby` text, `aria-label`, `labels[0]` text, `value` (for button inputs) or `alt`, `innerText`, `title`, `placeholder`. Collapse whitespace and cut to 80 chars.
- `value`: for `fill`, the current value or innerText (80 chars). For `select`, the selected option's label. `options` holds up to 20 option labels, for `select` only.
- `text`: the visible text nodes whose rect intersects the viewport, joined with `\n`, capped at 6000 chars.
- Actions cap: 250 actions in document order; `omitted` counts the rest.
- `resolve(n)`:
  - `stale` if the node is gone or disconnected
  - `hidden` if it fails the visibility or disabled checks
  - `offscreen` if its centre is outside the viewport
  - `covered` unless `e.contains(document.elementFromPoint(x, y))`
  - otherwise the centre `x`, `y` in CSS px. **Never** multiply by `devicePixelRatio`.

**Table format.** The exact format is pinned by the tests:
```
url: <url>
title: <title>
[e3] click button : Start generation
[e4] fill textbox : Prompt box = "a cat"
[e9] select combobox : Aspect ratio = "Portrait" {Portrait|Landscape|Square}
… 12 more actions not shown
scroll: up down
--- text ---
<text[:text_chars]>
```
- ` = "<value>"` appears only when the value is non-empty.
- `{…}` appears only when there are options.
- The `…` line appears only when `omitted > 0`.
- The `scroll:` line lists `up` and/or `down`, and is left out when neither applies.
- The text section is left out when `text_chars == 0` or the text is empty.

**Fixture `flow_like.html`.** It must contain each of these:
- a `div.ProseMirror[contenteditable=true][aria-label="Prompt box"]` holding `old text`
- `button[aria-label="Start generation"]`; clicking it appends `<p id="log">clicked:start</p>` and logs typed text
- `button` "Hidden" with `display:none`
- `div[aria-hidden=true] > button` "Ghost"
- `button[disabled]` "Disabled"
- `input[type=password][aria-label="Password"]` and `input[type=file][aria-label="Upload"]`
- `select[aria-label="Aspect ratio"]` with options Portrait (selected), Landscape and Square; `change` logs `aspect:<value>`
- `button#below` "Below fold" at `margin-top: 2000px`
- `button#remove-me` "Remove me", and `button#cover-me` "Cover me"
- a global `window.fixture = {remove(), cover(), showLater(ms, text)}`. `cover()` places a fixed full-screen `div` over everything; `showLater(ms, text)` appends `text` after `ms`.
- a `keydown` listener on the editor that logs `key:<key>`
- `a#away` linking to `http://localhost:<same port>/other.html`. The host is `localhost`, which is not `127.0.0.1`.

`other.html` holds a single button, "Secret elsewhere".

- [ ] **Step 1: Write the failing unit tests** in `tests/unit/test_browser_table.py`

```python
def test_render_table_exact_format():
    snap = Snapshot(url="https://flow.google.com/p", title="Flow", text="Hello world",
        actions=[Action("e3","click","button","Start generation"),
                 Action("e4","fill","textbox","Prompt box","a cat"),
                 Action("e9","select","combobox","Aspect ratio","Portrait",["Portrait","Landscape","Square"])],
        omitted=12, can_scroll_up=False, can_scroll_down=True)
    assert render_table(snap, text_chars=5) == (
        "url: https://flow.google.com/p\ntitle: Flow\n"
        "[e3] click button : Start generation\n"
        '[e4] fill textbox : Prompt box = "a cat"\n'
        '[e9] select combobox : Aspect ratio = "Portrait" {Portrait|Landscape|Square}\n'
        "… 12 more actions not shown\nscroll: down\n--- text ---\nHello")

def test_render_table_omits_empty_sections():   # omitted=0, no scroll, text_chars=0, value "" → no ' = ""'
    assert "…" not in t and "scroll:" not in t and "--- text ---" not in t and ' = ""' not in t

def test_parse_snapshot_round_trip():           # raw dict as produced by snapshot.js → Snapshot fields equal

def test_js_version_matches_python():
    assert f"version: {INDEX_VERSION}" in SNAPSHOT_JS or f"VERSION = {INDEX_VERSION}" in SNAPSHOT_JS

def test_js_is_read_only():
    for banned in ("fetch(", "XMLHttpRequest", "sendBeacon", "captureScreenshot"):
        assert banned not in SNAPSHOT_JS
    assert SNAPSHOT_JS.startswith("// Adapted from browser-use/jev-ultrafast (MIT)")
```

- [ ] **Step 2: Write the failing integration tests** in `tests/integration/test_browser_live.py`

```python
async def test_snapshot_lists_only_visible_safe_controls(cdp_page):
    c = await cdp_page("flow_like.html"); s = await take_snapshot(c)
    labels = {a.label for a in s.actions}
    assert {"Prompt box", "Start generation", "Aspect ratio"} <= labels
    assert not labels & {"Hidden", "Ghost", "Disabled", "Below fold", "Password", "Upload"}
    assert s.can_scroll_down and not s.can_scroll_up

async def test_ids_are_stable_across_snapshots(cdp_page):
    a = await take_snapshot(c); b = await take_snapshot(c)
    assert {x.label: x.id for x in a.actions} == {x.label: x.id for x in b.actions}

async def test_prompt_box_is_fill_with_value_and_select_has_options(cdp_page):
    assert prompt.kind == "fill" and prompt.value == "old text"
    assert aspect.kind == "select" and aspect.options == ["Portrait", "Landscape", "Square"]

async def test_snapshot_is_fast(cdp_page):   # median of 5 take_snapshot calls < 0.25 s (spec claims 10–60 ms; CI slack)
```

`conftest.py` details:
- Find the browser from `os.environ.get("CHROME_PATH")`, else `playwright.sync_api.sync_playwright().chromium.executable_path` (import inside `try`), else `/opt/pw-browsers/chromium*/chrome-linux/chrome` via glob. If none is found, `pytest.skip("no Chromium")`.
- Launch with `--headless=new --no-sandbox --remote-debugging-port=0 --user-data-dir=<tmp_path> --window-size=1200,800 about:blank`.
- Read the port from line 1 of `<tmp_path>/DevToolsActivePort`, polling for up to 10 s.
- Open the page with `Target.createTarget`, through a browser-level `CdpClient` on `/json/version` → `webSocketDebuggerUrl`.
- Then use `list_pages` plus `pick_page(..., ["127.0.0.1"])` and `CdpClient.connect`.
- Wait until `document.readyState == "complete"`, and terminate the process on teardown.

- [ ] **Step 3: Run** `python -m pytest tests/unit/test_browser_table.py tests/integration/test_browser_live.py -q`. Expected: FAIL (module missing).

- [ ] **Step 4: Implement** `snapshot.js` to the JS rules above, and `snapshot.py` to the Interfaces block. `SNAPSHOT_JS` is read from `Path(__file__).with_name("snapshot.js")` at import.

- [ ] **Step 5: Run** the same command. Expected: all PASS. (The integration tests SKIP only where no Chromium exists; in this cloud environment they must PASS.)

- [ ] **Step 6: Commit**

```bash
git add agent/browser/snapshot.js agent/browser/snapshot.py tests/unit/test_browser_table.py tests/integration tests/fixtures/browser
git commit -m "feat(browser): DOM-index snapshot (jev-style) and interaction table"
```

---

### Task 3: Steps and native input dispatch

**Files:**
- Create: `agent/browser/actions.py`
- Test: `tests/integration/test_browser_live.py` (append), `tests/unit/test_browser_table.py` (append the step-validation tests)

**Interfaces:**
- Consumes: `CdpClient.send/evaluate`, `CdpError` (Task 1); `index_expr`, `SNAPSHOT_JS` (Task 2).
- Produces:
  - Pydantic step models, each with an `op` literal:
    - `ClickStep(target)`
    - `FillStep(target, text: str max 4000)`
    - `SelectStep(target, option: str)`
    - `PressStep(key: Literal["Enter","Escape","Tab","Backspace"])`
    - `ScrollStep(dy: int, -2000..2000)`
    - `WaitStep(ms: int, 0..10000)`
    - `WaitForTextStep(text: str min 1, timeout_s: float, >0 and ≤600, default 60, absent: bool = False)`
  - `target` matches `^e\d+$` throughout.
  - `Step = Annotated[Union[...], Field(discriminator="op")]`
  - `@dataclass StepResult: op: str; ok: bool; error: str | None = None; detail: str | None = None`
  - `async def perform(client: CdpClient, step: Step, allowed_hosts: list[str]) -> StepResult`

**Dispatch values**, pinned:
- **Host gate.** Each step first evaluates `location.href`. If `host_allowed` is false, the step returns `HOST_NOT_ALLOWED` before doing anything else.
- **Click.**
  1. Evaluate `SNAPSHOT_JS + ";" + index_expr(f"resolve({n})")`. Re-injecting is a no-op when the version matches.
  2. Map the failure reasons: `stale`, `hidden` and `offscreen` give `STALE_ELEMENT`; `covered` gives `OBSCURED`.
  3. On success, dispatch `Input.dispatchMouseEvent` three times: `mouseMoved`, then `mousePressed` (`button="left", buttons=1, clickCount=1`), then `mouseReleased` (`button="left", buttons=0, clickCount=1`), all at `x`, `y`.
- **Fill.**
  1. Resolve as for Click. `kind != "fill"` gives `WRONG_KIND`.
  2. Click the element.
  3. Send `Input.dispatchKeyEvent` `keyDown` with `key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=(4 if sys.platform == "darwin" else 2), commands=["selectAll"]`, then `keyUp` with the same key and modifiers.
  4. If `text` is empty, press Backspace. Otherwise send `Input.insertText(text=text)`. There is no retry.
- **Press.** `keyDown` with `{key, code, windowsVirtualKeyCode, text}`, where `text` is sent only for Enter, then `keyUp`.

| key | code | VK | text |
|---|---|---|---|
| Enter | Enter | 13 | `"\r"` |
| Escape | Escape | 27 | — |
| Tab | Tab | 9 | — |
| Backspace | Backspace | 8 | — |

- **Scroll.** `mouseWheel` at the viewport centre (`innerWidth/2`, `innerHeight/2`) with `deltaX=0`, `deltaY=dy`. Then sleep 0.15 s.
- **Select.** Evaluate `index_expr(f"selectOption({n}, {json.dumps(option)})")`. Map `no_such_option` to `NO_SUCH_OPTION`, `wrong_kind` to `WRONG_KIND`, and `stale` to `STALE_ELEMENT`. The JS sets `e.value` and dispatches bubbling `input` and `change` events, as jev does.
- **Wait.** `asyncio.sleep(ms/1000)`.
- **WaitForText.** Poll `index_expr("visibleText()")` every 1.0 s until `text in visible` is true (or false, when `absent`). Give up with `WAIT_TIMEOUT` once `timeout_s` passes.
- **Disconnect.** A `CdpError` with code `CDP_CLOSED` inside a step returns `StepResult(op, False, "CDP_CLOSED")`. Any other `CdpError` propagates.

- [ ] **Step 1: Write the failing tests** (integration, appended)

```python
async def test_click_dispatches_real_click(cdp_page):
    await perform(c, ClickStep(op="click", target=start.id), ["127.0.0.1"])
    assert "clicked:start" in await c.evaluate("document.body.innerText")

async def test_fill_contenteditable_replaces_text_with_thai_and_emoji(cdp_page):
    text = "แมวน่ารักนั่งบนโต๊ะ 🐱\nline two"
    r = await perform(c, FillStep(op="fill", target=prompt.id, text=text), hosts)
    assert r.ok
    assert await c.evaluate("document.querySelector('.ProseMirror').innerText.trim()") == text

async def test_fill_on_button_is_wrong_kind(cdp_page): assert r.error == "WRONG_KIND"

async def test_select_option_fires_change(cdp_page):
    assert r.ok and "aspect:Landscape" in body
    r2 = await perform(c, SelectStep(op="select", target=aspect.id, option="Nope"), hosts)
    assert r2.error == "NO_SUCH_OPTION"

async def test_click_removed_element_is_stale(cdp_page):
    await c.evaluate("fixture.remove()")
    assert (await perform(c, ClickStep(op="click", target=remove_me.id), hosts)).error == "STALE_ELEMENT"

async def test_click_covered_element_is_obscured(cdp_page):
    await c.evaluate("fixture.cover()")
    r = await perform(c, ClickStep(op="click", target=start.id), hosts)
    assert r.error == "OBSCURED" and "clicked:start" not in await c.evaluate("document.body.innerText")

async def test_scroll_reveals_below_fold(cdp_page):
    await perform(c, ScrollStep(op="scroll", dy=2000), hosts)
    assert "Below fold" in {a.label for a in (await take_snapshot(c)).actions}

async def test_press_enter_reaches_focused_editor(cdp_page):   # fixture logs "key:Enter" on keydown in editor

async def test_wait_for_text_present_and_absent(cdp_page):
    await c.evaluate("fixture.showLater(1500, 'Generation complete')")
    assert (await perform(c, WaitForTextStep(op="wait_for_text", text="Generation complete", timeout_s=5), hosts)).ok
    assert (await perform(c, WaitForTextStep(op="wait_for_text", text="never", timeout_s=1.5), hosts)).error == "WAIT_TIMEOUT"

async def test_steps_refused_after_leaving_allowed_host(cdp_page):
    await perform(c, ClickStep(op="click", target=away.id), hosts)    # navigates to localhost
    await c.evaluate("new Promise(r => setTimeout(r, 500))")
    r = await perform(c, ClickStep(op="click", target="e1"), hosts)
    assert r.error == "HOST_NOT_ALLOWED"
```

Unit tests, appended to `test_browser_table.py`:

```python
def test_step_validation():
    TypeAdapter(Step).validate_python({"op": "click", "target": "e12"})
    for bad in [{"op":"click","target":"12"}, {"op":"scroll","dy":5000},
                {"op":"wait","ms":20000}, {"op":"press","key":"F5"},
                {"op":"wait_for_text","text":"x","timeout_s":601}, {"op":"eval","code":"1"}]:
        with pytest.raises(ValidationError): TypeAdapter(Step).validate_python(bad)

def test_actions_module_never_screenshots():
    assert "captureScreenshot" not in Path(actions.__file__).read_text()
```

- [ ] **Step 2: Run** `python -m pytest tests/integration/test_browser_live.py tests/unit/test_browser_table.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `actions.py` to the dispatch values above.
- [ ] **Step 4: Run** the same command. Expected: all PASS.
- [ ] **Step 5: Commit**

```bash
git add agent/browser/actions.py tests/integration/test_browser_live.py tests/unit/test_browser_table.py
git commit -m "feat(browser): native CDP input steps with stale/obscured/host guards"
```

---

### Task 4: Browser session (connect, reconnect, host gate, no replay)

**Files:**
- Create: `agent/browser/session.py`
- Test: `tests/unit/test_browser_session.py`

**Interfaces:**
- Consumes: `list_pages`, `pick_page`, `host_allowed`, `CdpClient`, `CdpError` (Task 1); `take_snapshot`, `render_table` (Task 2); `Step`, `perform`, `StepResult` (Task 3); `config.WS_PORT`, `config.FLOW_PROJECT_ID`.
- Produces:
  - `class BrowserSession.__init__(port: int, allowed_hosts: list[str], prefer_substring: str, timeout_s: float, *, ws_port: int, list_pages_fn=list_pages, connect_fn=CdpClient.connect, take_snapshot_fn=take_snapshot, perform_fn=perform)`
  - `async status() -> dict` returns `{"enabled": True, "port": int, "reachable": bool, "page_url": str | None, "error": str | None, "hint": str}` and never raises.
  - `async snapshot(text_chars: int = 600) -> dict` returns `{"url": str, "table": str, "actions": list[dict]}`
  - `async act(steps: list[Step], snapshot: bool = True, text_chars: int = 600) -> dict` returns `{"results": list[dict], "completed": int, "url": str, "table": str | None}`
  - `def get_browser_session() -> BrowserSession`, a module singleton built from config

Behaviour:
- A single `asyncio.Lock` wraps every public method.
- Before connecting, `port == ws_port` raises `CdpError("PORT_CONFLICT", ..., hint="FLOW_CDP_PORT must differ from WS_PORT (9222, the extension bridge); launch the browser with --remote-debugging-port=9333")`.
- `_ensure()` connects when there's no client or the client is closed. It runs `pick_page(await list_pages_fn(port), allowed_hosts, prefer_substring)`, then `connect_fn(page.ws_url, timeout_s)`.
- **Reconnection happens only inside `_ensure()`, before a call starts.**
  - `snapshot()`: on `CDP_CLOSED` it drops the client and retries once.
  - `act()`: never retries. A step that returns `CDP_CLOSED` stops the batch, and the client is dropped, so the next call reconnects.
- If the page's host is not allowed (checked with `location.href`), `snapshot()` returns `table = f"url: {url}\n(host not allowed — no actions)"` and `actions = []`, and never takes the DOM snapshot.
- `act` runs the steps in order and stops at the first `ok=False`. `completed` is the number of ok steps. The final table is taken when `snapshot` is true, using the same host rule.

- [ ] **Step 1: Write the failing tests** using fakes: a `FakeClient` with a `closed` flag and scripted `evaluate` results, plus a recording `perform_fn`.

```python
async def test_port_equal_to_ws_port_is_conflict():
    s = make(port=9222, ws_port=9222); st = await s.status()
    assert st["error"] == "PORT_CONFLICT" and "--remote-debugging-port=9333" in st["hint"]

async def test_status_unreachable_does_not_raise():   # list_pages_fn raises CDP_UNREACHABLE
    assert st["reachable"] is False and st["error"] == "CDP_UNREACHABLE" and st["hint"]

async def test_status_reports_page_url():  assert st["reachable"] and st["page_url"].startswith("https://flow.google.com/")

async def test_snapshot_on_disallowed_host_reveals_url_only():
    # location.href → "https://accounts.google.com/signin"
    assert r["actions"] == [] and r["table"] == "url: https://accounts.google.com/signin\n(host not allowed — no actions)"
    assert take_snapshot_fn.calls == 0

async def test_act_stops_at_first_failure():   # perform results ok, fail(OBSCURED), (third never called)
    assert r["completed"] == 1 and len(r["results"]) == 2 and perform_fn.calls == 2

async def test_act_does_not_replay_step_after_disconnect():
    # perform_fn returns StepResult("click", False, "CDP_CLOSED") and marks client closed
    r = await s.act([click("e3"), click("e4")])
    assert perform_fn.calls == 1 and r["results"][0]["error"] == "CDP_CLOSED"
    assert connect_fn.calls == 1                          # no reconnect inside act

async def test_next_call_reconnects_after_disconnect():
    ...; await s.snapshot(); assert connect_fn.calls == 2

async def test_snapshot_retries_once_on_closed():     # first take_snapshot raises CDP_CLOSED, second succeeds
    assert connect_fn.calls == 2 and r["table"].startswith("url: ")

async def test_calls_are_serialized():   # two concurrent act() calls: perform_fn never sees overlap (records enter/exit)

def test_singleton_uses_config(monkeypatch):  # FLOW_PROJECT_ID passed as prefer_substring, ws_port == config.WS_PORT
```

- [ ] **Step 2: Run** `python -m pytest tests/unit/test_browser_session.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `session.py` to the behaviour above.
- [ ] **Step 4: Run** the same command. Expected: all PASS.
- [ ] **Step 5: Commit**

```bash
git add agent/browser/session.py tests/unit/test_browser_session.py
git commit -m "feat(browser): session with host gate, port check and no-replay reconnect"
```

---

### Task 5: `/api/browser/*` routes

**Files:**
- Create: `agent/api/browser.py`
- Modify: `agent/main.py` (import, plus `app.include_router(browser_router, prefix="/api")` next to the other routers, around line 208)
- Test: `tests/unit/test_browser_api.py`

**Interfaces:**
- Consumes: `get_browser_session()`, `Step`, `CdpError` (Tasks 1, 3, 4); `config.FLOW_CDP_ENABLED`, read at request time through `from agent import config`, like `debug_gate.py` does.
- Produces:
  - `router = APIRouter(prefix="/browser", tags=["browser"], dependencies=[Depends(cdp_enabled)])`
  - `GET /api/browser/status` returns `session.status()`
  - `GET /api/browser/snapshot?text_chars=600` (`0 ≤ text_chars ≤ 6000`) returns `session.snapshot()`
  - `POST /api/browser/act` takes `ActRequest{steps: list[Step] (1..20), snapshot: bool = True, text_chars: int = 600}` and returns `session.act()`
  - `CdpError` maps to `HTTPException(status, {"code", "message", "hint"})`:

| code | HTTP status |
|---|---|
| `CDP_UNREACHABLE`, `CDP_CLOSED` | 503 |
| `CDP_TIMEOUT` | 504 |
| `NO_FLOW_PAGE`, `PORT_CONFLICT` | 409 |
| `JS_ERROR`, `CDP_PROTOCOL` | 502 |

  Step failures stay HTTP 200, carried in `results`.

- [ ] **Step 1: Write the failing tests.** Use `TestClient(main.app, base_url="http://127.0.0.1:8100")` without the lifespan, as in `test_api_security.py`, and monkeypatch `agent.api.browser.get_browser_session` with a fake.

```python
def test_routes_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "FLOW_CDP_ENABLED", False)
    for r in (client.get("/api/browser/status"), client.get("/api/browser/snapshot"),
              client.post("/api/browser/act", json={"steps":[{"op":"wait","ms":0}]})):
        assert r.status_code == 404 and "FLOW_CDP_ENABLED=1" in r.json()["detail"]

def test_snapshot_returns_table(enabled_client):  assert r.json()["table"].startswith("url: ")
def test_act_passes_steps_and_returns_results(enabled_client):   # fake records parsed steps; ops == ["fill","click"]
def test_act_rejects_empty_and_oversized_batches(enabled_client):  # [] and 21 steps → 422
def test_act_rejects_unknown_op(enabled_client):                   # {"op":"eval"} → 422
def test_cdp_error_mapping(enabled_client):    # parametrize code→status per table; body detail.code == code, detail.hint present
def test_cross_site_browser_call_refused(enabled_client):
    assert client.post("/api/browser/act", headers={"origin": "https://evil.example"}, json=...).status_code == 403
```

- [ ] **Step 2: Run** `python -m pytest tests/unit/test_browser_api.py -q`. Expected: FAIL (404 for every route, since the router isn't mounted).
- [ ] **Step 3: Implement** the router and mount it.
- [ ] **Step 4: Run** `python -m pytest tests/unit -q`. Expected: all PASS, including the existing suite.
- [ ] **Step 5: Commit**

```bash
git add agent/api/browser.py agent/main.py tests/unit/test_browser_api.py
git commit -m "feat(api): gated /api/browser status, snapshot and batched act"
```

---

### Task 6: `/fk-browser` skill, setup doc, doctor entries

**Files:**
- Create: `skills/fk-browser.md`, `docs/BROWSER_CDP.md`
- Modify:
  - `CLAUDE.md`: Skills table row, plus the new error codes in the "On any pipeline error" list
  - `skills/README.md`: table row
  - `README.md`: env table rows for the four `FLOW_CDP_*` settings
  - `skills/fk-doctor.md`: TRIGGER strings and a playbook per error code
- Generated: run `python scripts/sync_skills.py` to update `.agents/skills/`, `.claude/commands/` and `AGENTS.md`.

**Interfaces:**
- Consumes: the routes and codes from Task 5. The skill and docs quote them exactly.

Content decisions, which the writer must include:

**`docs/BROWSER_CDP.md`**
- Launch commands for macOS, Linux and Windows, for both Chrome and Brave, each using `--remote-debugging-port=9333 --user-data-dir=<home>/.flowkit-browser`. Explain why that flag is needed (Chrome 136+ ignores the debugging flag on the default profile), and say to sign in to `flow.google.com` once in that profile.
- An explicit "Do **not** add `--remote-allow-origins=*`" warning with the reason: any web page could then take over the signed-in browser.
- The fact that the port is reachable by any local process, so it should be closed when not in use.
- `FLOW_CDP_ENABLED=1`, and a pre-flight check with `curl -s http://127.0.0.1:8100/api/browser/status`, whose output must show `"reachable": true`.
- The extension can stay installed in the same profile. Batch-RPC features keep working alongside.

**`skills/fk-browser.md`.** The first line is the description: *Drive the Flow UI via DOM-indexed CDP snapshots (zero screenshots) — for Flow features the batch API lacks.*
- When to use:
  - an `UNSUPPORTED_ON_BATCH_API` error (video upscale, Veo r2v, start+end frame)
  - verifying what the Flow UI shows
  - one-off UI chores
- When NOT to use: anything the batch API covers. Use `/fk-gen-images` and `/fk-gen-videos` for those.
- The loop:
  1. `GET /api/browser/snapshot`
  2. choose targets from the table only, never guessing ids
  3. `POST /api/browser/act` with a batch, ending with `wait_for_text` where a result is expected
  4. read the returned table
- Rules:
  - Never take screenshots.
  - Re-snapshot after `STALE_ELEMENT` or `OBSCURED`; the usual cause of `OBSCURED` is a dialog that needs `press Escape`.
  - On `HOST_NOT_ALLOWED`, stop and ask the user. This is usually a sign-in page, and you never type credentials.
  - On `CDP_CLOSED`, check in the Flow UI whether the last step happened **before** you repeat it.
  - Before any click that starts a **Veo** generation or a video upscale, state the expected credit cost and get the user's go-ahead unless they already gave one for this batch.
  - Run one generation at a time, and check `GET /api/flow/status` for an active generation hold first.
- A worked example: "upscale scene 3's video via the UI". It shows a snapshot table excerpt, the act JSON, and a `wait_for_text` step. Label it illustrative, since the Flow labels are not yet captured.
- A follow-up note: once a UI flow has been proven, record its labels in `docs/CAPTURE.md` so it can become a recipe.

**`skills/fk-doctor.md`.** One row per code (`CDP_UNREACHABLE`, `PORT_CONFLICT`, `NO_FLOW_PAGE`, `HOST_NOT_ALLOWED`, `STALE_ELEMENT`, `OBSCURED`, `CDP_CLOSED`, `WAIT_TIMEOUT`, and `browser control disabled`). Each row gives the cause and the fix, taken from the hints above.

- [ ] **Step 1: Write the docs and skill** to the decisions above.
- [ ] **Step 2: Run** `python scripts/sync_skills.py && python scripts/validate_skills.py`. Expected: the validator reports no errors, and `.claude/commands/fk-browser.md` exists.
- [ ] **Step 3: Run** `grep -rn "remote-allow-origins=\*" docs skills CLAUDE.md README.md`. Expected: the only hits are inside the "Do not" warning.
- [ ] **Step 4: Run** `python -m pytest tests -q`. Expected: all PASS.
- [ ] **Step 5: Manual smoke test** (needs the user's machine):
  1. Launch Brave per the doc, then set `FLOW_CDP_ENABLED=1` and restart the agent.
  2. Call `GET /api/browser/status`; it should report `reachable: true` with the Flow URL.
  3. Call `GET /api/browser/snapshot`; it should list the prompt box and the generate button.
  4. Do one `fill` and `click` on an **image** generation (Nano Banana costs 0 credits) and see the image appear.
- [ ] **Step 6: Commit**

```bash
git add skills/fk-browser.md docs/BROWSER_CDP.md CLAUDE.md README.md skills/README.md skills/fk-doctor.md .agents .claude/commands AGENTS.md
git commit -m "docs(browser): /fk-browser skill, CDP setup guide, doctor entries"
```

---

## Follow-up (separate plan, after this ships)

**UI recipes for the unported Veo capabilities.** Use `/fk-browser` against the live Flow UI to record the labels and step sequence for video upscale, r2v and start+end frame. Then add `agent/browser/recipes/*.py` that the worker calls in place of failing with `UNSUPPORTED_ON_BATCH_API`. To link the result back, the worker diffs `RPC_PROJECT_MEDIA` before and after the run. This needs the captured labels, which can't be pinned in this plan.
