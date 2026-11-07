"""DOM-index snapshot: run snapshot.js in the page and render the interaction table."""
from dataclasses import dataclass
from pathlib import Path

from agent.browser.cdp import CdpClient, CdpError

SNAPSHOT_JS: str = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
INDEX_VERSION = 1


@dataclass
class Action:
    id: str
    kind: str
    role: str
    label: str
    value: str | None = None
    options: list[str] | None = None


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    actions: list[Action]
    omitted: int
    can_scroll_up: bool
    can_scroll_down: bool


def parse_snapshot(raw: dict) -> Snapshot:
    return Snapshot(
        url=raw.get("url", ""),
        title=raw.get("title", ""),
        text=raw.get("text", ""),
        actions=[
            Action(
                id=a["id"],
                kind=a["kind"],
                role=a.get("role", ""),
                label=a.get("label", ""),
                value=a.get("value"),
                options=a.get("options"),
            )
            for a in raw.get("actions", [])
        ],
        omitted=int(raw.get("omitted", 0)),
        can_scroll_up=bool(raw.get("can_scroll_up", False)),
        can_scroll_down=bool(raw.get("can_scroll_down", False)),
    )


async def take_snapshot(client: CdpClient) -> Snapshot:
    raw = await client.evaluate(SNAPSHOT_JS + "\n;window.__fkIndex.snapshot()")
    if not isinstance(raw, dict):
        raise CdpError("JS_ERROR", f"snapshot returned {type(raw).__name__}, not an object")
    return parse_snapshot(raw)


def render_table(snap: Snapshot, text_chars: int = 600) -> str:
    lines = [f"url: {snap.url}", f"title: {snap.title}"]
    for a in snap.actions:
        line = f"[{a.id}] {a.kind} {a.role} : {a.label}"
        if a.value:
            line += f' = "{a.value}"'
        if a.options:
            line += " {" + "|".join(a.options) + "}"
        lines.append(line)
    if snap.omitted > 0:
        lines.append(f"… {snap.omitted} more actions not shown")
    scroll = [d for d, on in (("up", snap.can_scroll_up), ("down", snap.can_scroll_down)) if on]
    if scroll:
        lines.append("scroll: " + " ".join(scroll))
    if text_chars > 0 and snap.text:
        lines.append("--- text ---")
        lines.append(snap.text[:text_chars])
    return "\n".join(lines)


def index_expr(call: str) -> str:
    return (
        f"(window.__fkIndex && window.__fkIndex.version === {INDEX_VERSION}) "
        f"? window.__fkIndex.{call} : {{ok:false, reason:'stale'}}"
    )
