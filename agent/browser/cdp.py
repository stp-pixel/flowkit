"""Minimal Chrome DevTools Protocol client (loopback only, no Origin header)."""
import asyncio
import itertools
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import websockets


class CdpError(RuntimeError):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class PageTarget:
    id: str
    url: str
    title: str
    ws_url: str


def host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    host = urlsplit(url).hostname
    return host is not None and host in allowed_hosts


async def list_pages(
    port: int, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[PageTarget]:
    try:
        async with httpx.AsyncClient(
            timeout=3, trust_env=False, transport=transport
        ) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json/list")
            resp.raise_for_status()
            entries = resp.json()
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise CdpError(
            "CDP_UNREACHABLE",
            f"cannot reach Chrome DevTools on 127.0.0.1:{port}: {exc}",
            hint=(
                f"Start the browser with --remote-debugging-port={port} "
                "--user-data-dir=<dedicated profile dir>; see docs/BROWSER_CDP.md"
            ),
        ) from exc
    return [
        PageTarget(
            id=e["id"],
            url=e.get("url", ""),
            title=e.get("title", ""),
            ws_url=e.get("webSocketDebuggerUrl", ""),
        )
        for e in entries
        if e.get("type") == "page"
    ]


def pick_page(
    pages: list[PageTarget], allowed_hosts: list[str], prefer_substring: str = ""
) -> PageTarget:
    allowed = [p for p in pages if host_allowed(p.url, allowed_hosts)]
    if not allowed:
        raise CdpError(
            "NO_FLOW_PAGE",
            f"no open page on an allowed host ({', '.join(allowed_hosts)})",
            hint="Open a signed-in https://flow.google.com/ tab in the debug browser.",
        )
    if prefer_substring:
        for p in allowed:
            if prefer_substring in p.url:
                return p
    return allowed[0]


class CdpClient:
    def __init__(self, ws: Any, timeout_s: float):
        self._ws = ws
        self._timeout_s = timeout_s
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._closed = False
        self._reader = asyncio.create_task(self._read_loop())

    @classmethod
    async def connect(cls, ws_url: str, timeout_s: float) -> "CdpClient":
        try:
            # proxy=None: the DevTools socket is loopback only and must never be
            # routed through HTTPS_PROXY (websockets >= 15 honours it by default).
            ws = await websockets.connect(
                ws_url, max_size=16 * 2**20, open_timeout=timeout_s, proxy=None
            )
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            raise CdpError("CDP_UNREACHABLE", f"cannot open {ws_url}: {exc}") from exc
        return cls(ws, timeout_s)

    @property
    def closed(self) -> bool:
        return self._closed

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue
                fut = self._pending.pop(mid, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except websockets.WebSocketException:
            pass
        finally:
            self._closed = True
            pending, self._pending = self._pending, {}
            for fut in pending.values():
                if not fut.done():
                    fut.set_exception(
                        CdpError("CDP_CLOSED", "CDP connection closed")
                    )

    async def send(
        self, method: str, params: dict | None = None, timeout_s: float | None = None
    ) -> dict:
        if self._closed:
            raise CdpError("CDP_CLOSED", "CDP connection closed")
        mid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._ws.send(
                json.dumps({"id": mid, "method": method, "params": params or {}})
            )
            msg = await asyncio.wait_for(
                fut, timeout_s if timeout_s is not None else self._timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise CdpError("CDP_TIMEOUT", f"{method} timed out") from exc
        except websockets.WebSocketException as exc:
            raise CdpError("CDP_CLOSED", f"CDP connection closed: {exc}") from exc
        finally:
            self._pending.pop(mid, None)
        if "error" in msg:
            raise CdpError(
                "CDP_PROTOCOL", str(msg["error"].get("message", msg["error"]))
            )
        return msg.get("result", {})

    async def evaluate(self, expression: str) -> Any:
        res = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        details = res.get("exceptionDetails")
        if details:
            text = (details.get("exception") or {}).get("description") or details.get(
                "text", "evaluation failed"
            )
            raise CdpError("JS_ERROR", text)
        return res.get("result", {}).get("value")

    async def close(self) -> None:
        self._closed = True
        await self._ws.close()
        await asyncio.gather(self._reader, return_exceptions=True)
