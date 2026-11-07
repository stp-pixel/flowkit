# DOM-Indexed Browser Control (CDP) — Design

Date: 2026-11-07
Source: user request to adapt [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)
(MIT) to Flow Kit, following a write-up of the "DOM-index, zero-screenshot" technique.

## Problem

Flow Kit reaches Google Flow in one way only: the extension runs signed
`batchexecute` RPCs inside the Flow tab (`agent/services/flow_batch.py`). That
works for every RPC whose payload was captured. It cannot do anything whose
payload was never captured: **video upscale**, **Veo r2v**, and **Veo
start+end-frame chaining** fail with `UNSUPPORTED_ON_BATCH_API`. It also cannot
operate the parts of the Flow UI the RPC layer doesn't model.

The two usual alternatives are both poor fits. Screenshot-driven computer use
is slow and costs tens of thousands of vision tokens per task. More hand-built
in-page `fetch()` calls are blocked on payload captures.

## Idea (from jev-ultrafast)

Drive the **real Flow UI** through the Chrome DevTools Protocol, without taking
screenshots:

1. **Snapshot.** One `Runtime.evaluate` runs a short script. The script lists
   only the controls a person can see in the viewport right now (buttons,
   links, inputs, contenteditable, ARIA widgets), and gives each one an id. It
   drops anything that is hidden, `aria-hidden`, `inert`, or disabled, and it
   never lists password or file inputs.
2. **Table.** The snapshot is rendered as a few hundred characters of text,
   such as `[e3] click button : Start generation`. Any text model can read it;
   no vision model is needed.
3. **Act.** The chosen action is sent as native input events:
   `Input.dispatchMouseEvent` at the element's centre (in CSS pixels, so OS
   scaling doesn't matter), and `Input.insertText` / `Input.dispatchKeyEvent`
   for typing. The page then issues its own requests, signed with its own
   tokens.

The "AI chooses an action" step is the Claude session running Flow Kit
skills. Flow Kit serves the snapshot and executes actions. It does not embed
its own LLM.

## Requirements

- R1. Connect to a Chromium-family browser (Chrome, Brave, Edge) over CDP at
  `127.0.0.1:<FLOW_CDP_PORT>`, default **9333**. Port 9222 is already the
  agent's extension WebSocket (`WS_PORT`). Loopback only.
- R2. Attach only to page targets whose hostname is in
  `FLOW_CDP_ALLOWED_HOSTS` (default `flow.google.com`). Prefer a tab whose URL
  contains `FLOW_PROJECT_ID`.
- R3. Off by default. Every route answers 404 unless `FLOW_CDP_ENABLED=1`.
- R4. `GET /api/browser/snapshot` returns the interaction table and the
  structured actions.
- R5. `POST /api/browser/act` runs a batch of 1–20 steps in order. The step
  ops are `click`, `fill`, `select`, `press`, `scroll`, `wait` and
  `wait_for_text`. It stops at the first failed step and returns per-step
  results plus a fresh table.
- R6. Never call `Page.captureScreenshot`. Injected JS only reads the DOM and
  never issues network requests.
- R7. A step whose target was removed or hidden, or is covered by another
  element, fails without dispatching any input.
- R8. Once the tab has left an allowed host, every later step is refused and
  the snapshot reveals only the URL.
- R9. A step is never retried after its input may have reached the page. A
  retried click on "Generate" spends Veo credits twice.
- R10. A `/fk-browser` skill teaches the snapshot → choose → act loop and its
  credit and safety rules. Setup, `/fk-doctor` and the docs cover the new
  error codes.
- R11. No new runtime dependencies. `websockets` and `httpx` are already in
  `requirements.txt`.

## Non-goals (this spec)

- Hard-coded Flow UI recipes that the worker runs automatically, such as
  "upscale via UI" as a fallback for `UNSUPPORTED_ON_BATCH_API`. That needs
  the live Flow labels captured first, and is a follow-up plan built on this
  engine.
- iframes, closed shadow roots, canvas widgets, file uploads through the UI,
  and pop-up windows. jev-ultrafast leaves these out of scope too.
- Telegram or other remote command channels.
- Any attempt to disguise automation. This drives the real UI at human pace.
  Flow's rate limits and the existing generation hold still apply.
