"""Steps against the DOM index, dispatched as native CDP input (never screenshots)."""
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from agent.browser.cdp import CdpClient, CdpError, host_allowed
from agent.browser.snapshot import SNAPSHOT_JS, index_expr


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")


Target = Annotated[str, Field(pattern=r"^e[0-9]{1,9}$")]


class ClickStep(_Step):
    op: Literal["click"]
    target: Target


class FillStep(_Step):
    op: Literal["fill"]
    target: Target
    text: str = Field(max_length=4000)


class SelectStep(_Step):
    op: Literal["select"]
    target: Target
    option: str


class PressStep(_Step):
    op: Literal["press"]
    key: Literal["Enter", "Escape", "Tab", "Backspace"]


class ScrollStep(_Step):
    op: Literal["scroll"]
    dy: int = Field(ge=-2000, le=2000)


class WaitStep(_Step):
    op: Literal["wait"]
    ms: int = Field(ge=0, le=10000)


class WaitForTextStep(_Step):
    op: Literal["wait_for_text"]
    text: str = Field(min_length=1)
    timeout_s: float = Field(default=60, gt=0, le=600)
    absent: bool = False


Step = Annotated[
    Union[ClickStep, FillStep, SelectStep, PressStep, ScrollStep, WaitStep, WaitForTextStep],
    Field(discriminator="op"),
]


@dataclass
class StepResult:
    op: str
    ok: bool
    error: str | None = None
    detail: str | None = None


# key -> (code, windowsVirtualKeyCode, text sent with keyDown)
KEYS: dict[str, tuple[str, int, str | None]] = {
    "Enter": ("Enter", 13, "\r"),
    "Escape": ("Escape", 27, None),
    "Tab": ("Tab", 9, None),
    "Backspace": ("Backspace", 8, None),
}
RESOLVE_ERRORS = {
    "stale": "STALE_ELEMENT",
    "hidden": "STALE_ELEMENT",
    "offscreen": "STALE_ELEMENT",
    "covered": "OBSCURED",
}
SELECT_ERRORS = {
    "stale": "STALE_ELEMENT",
    "hidden": "STALE_ELEMENT",
    "no_such_option": "NO_SUCH_OPTION",
    "wrong_kind": "WRONG_KIND",
}
WAIT_POLL_S = 1.0
SCROLL_SETTLE_S = 0.15


async def _index(client: CdpClient, call: str) -> Any:
    # Re-inject on every call: a no-op while the version matches, and it rebuilds
    # the index after a navigation or re-render whichever step runs first.
    return await client.evaluate(SNAPSHOT_JS + "\n;" + index_expr(call))


def _failed(op: str, raw: Any, errors: dict[str, str]) -> StepResult:
    reason = raw.get("reason") if isinstance(raw, dict) else None
    return StepResult(op, False, errors.get(reason, "STALE_ELEMENT"), reason)


async def _mouse_click(client: CdpClient, x: float, y: float) -> None:
    await client.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
        await client.send(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": "left",
             "buttons": buttons, "clickCount": 1},
        )


async def _press(client: CdpClient, key: str) -> None:
    code, vk, text = KEYS[key]
    down = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk}
    if text is not None:
        down["text"] = text
    await client.send("Input.dispatchKeyEvent", down)
    await client.send(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk},
    )


async def _select_all(client: CdpClient) -> None:
    key = {"key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65,
           "modifiers": 4 if sys.platform == "darwin" else 2}
    await client.send("Input.dispatchKeyEvent",
                      {"type": "keyDown", **key, "commands": ["selectAll"]})
    await client.send("Input.dispatchKeyEvent", {"type": "keyUp", **key})


async def _resolve(client: CdpClient, step: ClickStep | FillStep) -> dict | StepResult:
    raw = await _index(client, f"resolve({int(step.target[1:])})")
    if not (isinstance(raw, dict) and raw.get("ok")):
        return _failed(step.op, raw, RESOLVE_ERRORS)
    return raw


async def _dispatch(client: CdpClient, step: Any) -> StepResult:
    op = step.op
    if isinstance(step, (ClickStep, FillStep)):
        hit = await _resolve(client, step)
        if isinstance(hit, StepResult):
            return hit
        if isinstance(step, FillStep) and hit.get("kind") != "fill":
            return StepResult(op, False, "WRONG_KIND", hit.get("kind"))
        await _mouse_click(client, hit["x"], hit["y"])
        if isinstance(step, FillStep):
            await _select_all(client)
            if step.text:
                await client.send("Input.insertText", {"text": step.text})
            else:
                await _press(client, "Backspace")
        return StepResult(op, True)
    if isinstance(step, SelectStep):
        n = int(step.target[1:])
        raw = await _index(client, f"selectOption({n}, {json.dumps(step.option)})")
        if not (isinstance(raw, dict) and raw.get("ok")):
            return _failed(op, raw, SELECT_ERRORS)
        return StepResult(op, True)
    if isinstance(step, PressStep):
        await _press(client, step.key)
        return StepResult(op, True)
    if isinstance(step, ScrollStep):
        size = await client.evaluate("[innerWidth, innerHeight]")
        await client.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": size[0] / 2, "y": size[1] / 2,
             "deltaX": 0, "deltaY": step.dy},
        )
        await asyncio.sleep(SCROLL_SETTLE_S)
        return StepResult(op, True)
    if isinstance(step, WaitStep):
        await asyncio.sleep(step.ms / 1000)
        return StepResult(op, True)
    if isinstance(step, WaitForTextStep):
        deadline = time.monotonic() + step.timeout_s
        while True:
            visible = await _index(client, "visibleText()")
            present = isinstance(visible, str) and step.text in visible
            if present != step.absent:
                return StepResult(op, True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return StepResult(op, False, "WAIT_TIMEOUT")
            await asyncio.sleep(min(WAIT_POLL_S, remaining))
    raise TypeError(f"unknown step {type(step).__name__}")


async def perform(client: CdpClient, step: Step, allowed_hosts: list[str]) -> StepResult:
    try:
        href = await client.evaluate("location.href")
        if not host_allowed(href or "", allowed_hosts):
            return StepResult(step.op, False, "HOST_NOT_ALLOWED", href)
        return await _dispatch(client, step)
    except CdpError as exc:
        if exc.code == "CDP_CLOSED":
            return StepResult(step.op, False, "CDP_CLOSED", exc.message)
        raise
