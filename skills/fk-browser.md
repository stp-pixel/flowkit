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
2. Choose targets **from the table only**. Never guess an id.
3. `POST /api/browser/act` with a batch of steps, ending with `wait_for_text` when a result is expected.
4. Read the returned `table` (it is the new snapshot) and decide the next batch.

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
| `wait_for_text` | `text`, `timeout_s` (max 600, default 60), `absent` (wait for the text to disappear) |

Targets look like `e3` (`e` plus digits). The response is `{results: [{op, ok, error, detail}], completed, url, table}`, plus `table_error` if the final table could not be built (then `table` is null).

Multi-line prompts: Flow's editor stores each line as a paragraph, and `fill` types the text as given, so newlines become paragraphs. Whether `Enter` submits in the Flow prompt box is unverified: click the generate button instead of `press Enter`.

## Rules

- **Never take screenshots.** The table is the interface.
- After `STALE_ELEMENT` or `OBSCURED`, re-snapshot before anything else. The usual cause of `OBSCURED` is a dialog: `press Escape`, then re-snapshot.
- On `HOST_NOT_ALLOWED`, stop and ask the user. It is usually a sign-in page, and you never type credentials.
- On `CDP_CLOSED` or `CDP_TIMEOUT` mid-batch, the step may or may not have reached the page. Check in the Flow UI (snapshot) whether it happened **before** you repeat it. Steps are never retried automatically. `JS_ERROR` and `CDP_PROTOCOL` mid-batch are likewise reported per step.
- Once a batch has started, `act` returns HTTP 200 and the failure is in `results` (check `completed` and each `ok`). Only connection-level failures before any step give an HTTP error with `{code, message, hint}`.
- Before any click that starts a **Veo** generation or a video upscale, state the expected credit cost and get the user's go-ahead, unless they already gave one for this batch. Image generation costs 0 credits.
- One generation at a time. First check `GET /api/flow/status` for an active generation hold.

## Errors

Invoke `/fk-doctor` for any code. Quick map: `CDP_UNREACHABLE`, `PORT_CONFLICT`, `NO_FLOW_PAGE` mean setup (see the doc); `STALE_ELEMENT`, `OBSCURED`, `WRONG_KIND`, `NO_SUCH_OPTION` mean re-snapshot and pick again; `WAIT_TIMEOUT` means the expected text never appeared in time.

## Worked example (illustrative)

Task: upscale scene 3's video via the UI. This is **illustrative only**: the real Flow labels and element ids have not been captured, so placeholders stand in for them.

1. Snapshot, then find the scene 3 video and its menu in the table:

```
[e12] click button : <Scene 3 video card>
[e15] click button : <Video options menu label>
```

2. Open the menu, choose upscale, and wait for the result:

```json
{"steps": [
  {"op": "click", "target": "e12"},
  {"op": "click", "target": "e15"},
  {"op": "click", "target": "e21"},
  {"op": "wait_for_text", "text": "<Upscale done text>", "timeout_s": 300}
]}
```

`e21` stands for whatever `<Upscale button label>` is in the snapshot taken after the menu opens. In practice, send the first two clicks, read the new table, then pick the upscale target from it. Get the user's go-ahead on the credit cost before the click that starts the upscale.

## After it works

Once a UI flow has been proven, record its labels and step sequence in [docs/CAPTURE.md](docs/CAPTURE.md) so it can become a recipe.
