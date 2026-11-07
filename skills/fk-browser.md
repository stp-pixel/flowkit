Drive the Flow UI via DOM-indexed CDP snapshots (zero screenshots) — for Flow features the batch API lacks.

## When to use

- A request or call fails with `UNSUPPORTED_ON_BATCH_API` (video upscale, Veo r2v, start+end frame)
- Verifying what the Flow UI actually shows
- One-off UI chores in the signed-in Flow tab

## When NOT to use

Anything the batch API covers. Use `/fk-gen-images` and `/fk-gen-videos` for those; they are faster, safer and tracked in the database.

## Pre-flight

Setup is in [docs/BROWSER_CDP.md](docs/BROWSER_CDP.md): a browser launched with a dedicated profile and `--remote-debugging-port=9333`, plus `FLOW_CDP_ENABLED=1` on the agent.

```bash
curl -s http://127.0.0.1:8100/api/browser/status
# Must show "reachable": true and a flow.google.com page_url
```

A 404 `browser control disabled; set FLOW_CDP_ENABLED=1` means the feature is off. If `reachable` is false, read `error` and `hint`, then invoke `/fk-doctor`. Never add `--remote-allow-origins=*`.

## The loop

1. `GET /api/browser/snapshot` (optional `?text_chars=600`, 0 to 6000). Returns `{url, table, actions}`.
2. Choose targets **from the action lines only** (`[eN] …`). Never take an id from the `--- text ---` section, and never guess one.
3. `POST /api/browser/act` with a batch of steps. A batch that clicks something that spends credits ends at that click (no wait, or a short `wait` of a few seconds).
4. Read the returned `table` (it is the new snapshot) and decide the next batch. To wait for a long job, poll with separate act calls (see "Waiting for long jobs").

Always call the API with a client timeout, e.g. `curl -s --max-time 110 …`, so the command ends before the Bash tool's 120 s limit kills it.

Table lines look like:

```
[e3] click button : Start generation
[e4] fill textbox : Prompt box = "a cat"
[e9] select combobox : Aspect ratio = "Portrait" {Portrait|Landscape|Square}
```

### Step ops

`POST /api/browser/act` body: `{"steps": [...1 to 20...], "snapshot": true, "text_chars": 600}`. Unknown fields give 422.

| op | Fields |
|----|--------|
| `click` | `target` |
| `fill` | `target`, `text` (max 4000) |
| `select` | `target`, `option` |
| `press` | `key`: `Enter`, `Escape`, `Tab` or `Backspace` |
| `scroll` | `dy` (-2000 to 2000) |
| `wait` | `ms` (0 to 10000) |
| `wait_for_text` | `text`, `timeout_s` (default 60), `absent` (wait for the text to disappear) |

Targets look like `e3` (`e` plus digits). The response is `{results: [{op, ok, error, detail}], completed, url, table}`, plus `table_error` if the final table could not be built (then `table` is null).

The total waiting in one batch (every `wait` `ms` / 1000 plus every `wait_for_text` `timeout_s`) may not exceed 120 s; a larger batch is refused with 422 before any step runs.

`wait_for_text` matches only text that is on screen: the same first 6000 characters the `--- text ---` section is cut from, with each element's whitespace collapsed to single spaces and separate elements on separate lines. Scroll the target area into view first if needed, and keep the needle a short, single-spaced phrase from one element.

Multi-line prompts: Flow's editor stores each line as a paragraph, and `fill` types the text as given, so newlines become paragraphs. Whether `Enter` submits in the Flow prompt box is unverified: click the generate button instead of `press Enter`.

### Waiting for long jobs

Never put a credit-spending click and a long wait in one act. Send the click batch with no wait or a short one, then poll in **separate** act calls, each a single `wait_for_text` with `timeout_s` of at most 90 and `curl --max-time 110`. On `WAIT_TIMEOUT`, read the returned table and poll again; the job is usually still running.

**If an act response was lost (timeout, killed command, no output), never resend it.** `GET /api/browser/snapshot` first and work out from the table what already happened. Resending a lost batch can click Generate or Upscale a second time.

## Rules

- **Never take screenshots.** The table is the interface.
- After `STALE_ELEMENT` or `OBSCURED`, re-snapshot before anything else. The usual cause of `OBSCURED` is a dialog: `press Escape`, then re-snapshot.
- On `HOST_NOT_ALLOWED`, or a snapshot whose table reads `(host not allowed — no actions)`, stop and ask the user. It is usually a sign-in page, and you never type credentials. `wait_for_text` also stops with `HOST_NOT_ALLOWED` if the page leaves Flow mid-wait.
- On `CDP_CLOSED` or `CDP_TIMEOUT` mid-batch, the step may or may not have reached the page. Check in the Flow UI (snapshot) whether it happened **before** you repeat it. Steps are never retried automatically. `JS_ERROR` and `CDP_PROTOCOL` mid-batch are likewise reported per step.
- Once a batch has started, `act` returns HTTP 200 and the failure is in `results` (check `completed` and each `ok`). Only connection-level failures before any step give an HTTP error with `{code, message, hint}`.
- Before any click that spends credits, state the expected cost and get the user's go-ahead, unless they already gave one for this batch. Under Flow's current Google AI Pro plan only Nano Banana image generation is unmetered; other image models and every Veo generation or upscale spend credits. When unsure which model the UI has selected, or what it costs, ask.
- Before any generate or upscale click, check `GET /api/flow/status`. Refuse the click while `generation_throttle.cooldown_active` is `true`.
- Do not start a UI generation while the worker is busy: `GET /api/requests?status=PROCESSING` must return `[]`. One generation at a time.

## Errors

Invoke `/fk-doctor` for any code. Quick map: `CDP_UNREACHABLE`, `PORT_CONFLICT`, `NO_FLOW_PAGE` mean setup (see the doc); `STALE_ELEMENT`, `OBSCURED`, `WRONG_KIND`, `NO_SUCH_OPTION` mean re-snapshot and pick again; `WAIT_TIMEOUT` means the expected text never appeared in time; `INTERNAL` is a Flow Kit bug (the batch stopped and the connection was reset): snapshot to see what happened, never resend the batch, and report the `detail`.

## Worked example (illustrative)

Task: upscale scene 3's video via the UI. This is **illustrative only**: the real Flow labels and element ids have not been captured, so placeholders stand in for them.

1. Pre-checks: `generation_throttle.cooldown_active` is `false` in `GET /api/flow/status`, `GET /api/requests?status=PROCESSING` returns `[]`, and the user has approved the upscale's credit cost.

2. Snapshot, then find the scene 3 video and its menu in the action lines:

```
[e12] click button : <Scene 3 video card>
[e15] click button : <Video options menu label>
```

3. Open the menu (no credits spent yet), and read the new table:

```json
{"steps": [
  {"op": "click", "target": "e12"},
  {"op": "click", "target": "e15"}
]}
```

4. Pick the upscale entry from the table returned by step 3, say `[e21] click menuitem : <Upscale button label>`, and click it on its own, with at most a short wait:

```json
{"steps": [
  {"op": "click", "target": "e21"},
  {"op": "wait", "ms": 2000}
]}
```

5. Poll in separate calls until the result shows, each well inside the client timeout:

```bash
curl -s --max-time 110 -X POST http://127.0.0.1:8100/api/browser/act \
  -H 'Content-Type: application/json' \
  -d '{"steps": [{"op": "wait_for_text", "text": "<Upscale done text>", "timeout_s": 90}]}'
```

Repeat step 5 on `WAIT_TIMEOUT`. If any of these calls loses its response, do not resend it: snapshot and read what happened.

## After it works

Once a UI flow has been proven, record its labels and step sequence in [docs/CAPTURE.md](docs/CAPTURE.md) so it can become a recipe.
