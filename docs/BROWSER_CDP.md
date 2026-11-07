# Browser control over CDP

Flow Kit can read and drive the Flow web UI through the Chrome DevTools Protocol
(CDP). It builds a numbered table of the page's interactive elements (the DOM
index), so a Claude session works from text and never needs a screenshot.
Use it for Flow features the batch API does not cover. See the `/fk-browser` skill.

It is off by default. Nothing under `/api/browser/*` works until you enable it
and start a browser with a debugging port.

## 1. Launch the browser

Start Chrome or Brave with a **dedicated profile directory**:

```
--remote-debugging-port=9333 --user-data-dir=<home>/.flowkit-browser
```

The dedicated `--user-data-dir` is required. Chrome 136 and later ignores
`--remote-debugging-port` on the default profile, so without it the port never opens.
Port 9333 is the `FLOW_CDP_PORT` default. Never use 9222: that is `WS_PORT`, the
extension bridge.

On first launch, sign in to `https://flow.google.com/` once in that profile. The
login is kept in the profile directory for later launches.

### macOS

```bash
# Chrome
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9333 --user-data-dir="$HOME/.flowkit-browser" https://flow.google.com/

# Brave
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --remote-debugging-port=9333 --user-data-dir="$HOME/.flowkit-browser" https://flow.google.com/
```

### Linux

```bash
# Chrome
google-chrome --remote-debugging-port=9333 --user-data-dir="$HOME/.flowkit-browser" https://flow.google.com/

# Brave
brave-browser --remote-debugging-port=9333 --user-data-dir="$HOME/.flowkit-browser" https://flow.google.com/
```

### Windows (PowerShell)

```powershell
# Chrome
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9333 --user-data-dir="$env:USERPROFILE\.flowkit-browser" https://flow.google.com/

# Brave
& "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" `
  --remote-debugging-port=9333 --user-data-dir="$env:USERPROFILE\.flowkit-browser" https://flow.google.com/
```

Adjust the executable path if your browser is installed elsewhere.

## 2. Security

- Do **not** add `--remote-allow-origins=*`. It is not needed: the Flow Kit client
  sends no `Origin` header. With `*`, any web page you visit could connect to the
  debugging port and take over the signed-in browser.
- The debugging port is reachable by **any local process** on your machine, and
  whoever connects controls the signed-in session. Close the browser (or launch it
  without the flag) when you are not using browser control.
- Flow Kit connects to `127.0.0.1` only, and only to pages on the hosts in
  `FLOW_CDP_ALLOWED_HOSTS` (default `flow.google.com`). A step that finds the
  page on another host fails with `HOST_NOT_ALLOWED`, and a snapshot there shows
  only the URL and `(host not allowed — no actions)`.

## 3. Enable and check

```bash
export FLOW_CDP_ENABLED=1     # then restart the agent
curl -s http://127.0.0.1:8100/api/browser/status
```

The output must show `"reachable": true` and a `page_url` on `flow.google.com`.
`status` never errors; when something is wrong it reports `error` and `hint`.
While `FLOW_CDP_ENABLED` is off, every `/api/browser/*` route returns 404 with
`browser control disabled; set FLOW_CDP_ENABLED=1`.

| Env var | Default | What it does |
|---------|---------|--------------|
| `FLOW_CDP_ENABLED` | `0` | `1` turns on the `/api/browser/*` routes. |
| `FLOW_CDP_PORT` | `9333` | The browser's debugging port. Must differ from `WS_PORT` (9222), else `PORT_CONFLICT`. |
| `FLOW_CDP_ALLOWED_HOSTS` | `flow.google.com` | Comma-separated exact hostnames the page may be on (case-insensitive). |
| `FLOW_CDP_TIMEOUT_S` | `10` | Timeout for each CDP call. |

## 4. The extension

The dedicated profile starts empty: it has none of your usual extensions. If you
want the batch path (image and video generation, uploads) in the same browser,
load the Flow Kit extension into it once: open `chrome://extensions` (Brave:
`brave://extensions`), turn on Developer mode, click **Load unpacked** and pick
this repository's `extension/` folder. After that, batch-RPC features keep working
alongside browser control, as long as the signed-in Flow tab stays open.

## 5. Troubleshooting

Run `/fk-doctor`. The common ones:

- `CDP_UNREACHABLE`: the browser is not running with the flag, or the profile is the default one.
- `NO_FLOW_PAGE`: no Flow tab is open, or DevTools (or another debugger) is attached to it. Close DevTools.
- `PORT_CONFLICT`: `FLOW_CDP_PORT` equals 9222. Use 9333.
