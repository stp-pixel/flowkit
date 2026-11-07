"""Browser session: one lazily (re)connected CDP client, a host gate, no step replay."""
import asyncio
import dataclasses

from agent import config
from agent.browser.actions import Step, StepResult, perform
from agent.browser.cdp import CdpClient, CdpError, host_allowed, list_pages, pick_page
from agent.browser.snapshot import render_table, take_snapshot


class BrowserSession:
    def __init__(
        self,
        port: int,
        allowed_hosts: list[str],
        prefer_substring: str,
        timeout_s: float,
        *,
        ws_port: int,
        list_pages_fn=list_pages,
        connect_fn=CdpClient.connect,
        take_snapshot_fn=take_snapshot,
        perform_fn=perform,
    ):
        self._port = port
        self._allowed_hosts = allowed_hosts
        self._prefer = prefer_substring
        self._timeout_s = timeout_s
        self._ws_port = ws_port
        self._list_pages = list_pages_fn
        self._connect = connect_fn
        self._take_snapshot = take_snapshot_fn
        self._perform = perform_fn
        self._client = None
        self._last_url = ""
        self._lock = asyncio.Lock()

    def _unreachable_hint(self) -> str:
        return (
            f"Start the browser with --remote-debugging-port={self._port} "
            "--user-data-dir=<dedicated profile dir>; see docs/BROWSER_CDP.md"
        )

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def _ensure(self):
        if self._port == self._ws_port:
            raise CdpError(
                "PORT_CONFLICT",
                f"FLOW_CDP_PORT {self._port} equals WS_PORT (the extension bridge)",
                hint="FLOW_CDP_PORT must differ from WS_PORT (9222, the extension bridge); "
                "launch the browser with --remote-debugging-port=9333",
            )
        if self._client is not None and not self._client.closed:
            return self._client
        await self._drop_client()
        try:
            pages = await self._list_pages(self._port)
            flow = [p for p in pages if host_allowed(p.url, self._allowed_hosts)]
            if flow and not any(p.ws_url for p in flow):
                raise CdpError(
                    "NO_FLOW_PAGE",
                    "the Flow tab has no debugger endpoint available",
                    hint="Close DevTools or any other debugger attached to the Flow tab",
                )
            usable = [p for p in pages if p.ws_url]
            page = pick_page(usable, self._allowed_hosts, self._prefer)
            self._client = await self._connect(page.ws_url, self._timeout_s)
        except CdpError:
            raise
        except Exception as exc:
            raise CdpError(
                "CDP_UNREACHABLE", str(exc) or type(exc).__name__,
                hint=self._unreachable_hint(),
            ) from exc
        self._last_url = page.url
        return self._client

    @staticmethod
    def _url_only(url: str) -> tuple[str, str, list[dict]]:
        return url, f"url: {url}\n(host not allowed — no actions)", []

    async def _table(self, client, text_chars: int) -> tuple[str, str, list[dict]]:
        href = await client.evaluate("location.href") or ""
        self._last_url = href
        if not host_allowed(href, self._allowed_hosts):
            return self._url_only(href)
        snap = await self._take_snapshot(client)
        # The page may have navigated between the two evaluates: judge the
        # snapshot by the URL it was taken on, and reveal nothing else off-host.
        self._last_url = snap.url or href
        if not host_allowed(snap.url, self._allowed_hosts):
            return self._url_only(snap.url)
        return (
            snap.url,
            render_table(snap, text_chars),
            [dataclasses.asdict(a) for a in snap.actions],
        )

    async def status(self) -> dict:
        async with self._lock:
            out = {
                "enabled": True, "port": self._port, "reachable": False,
                "page_url": None, "error": None, "hint": "",
            }
            try:
                client = await self._ensure()
                out["page_url"] = await client.evaluate("location.href")
                out["reachable"] = True
            except CdpError as exc:
                out["error"], out["hint"] = exc.code, exc.hint
                if exc.code == "CDP_CLOSED":
                    await self._drop_client()
            except Exception as exc:  # status never raises
                out["error"] = "CDP_UNREACHABLE"
                out["hint"] = self._unreachable_hint()
                out["detail"] = str(exc)
            return out

    async def snapshot(self, text_chars: int = 600) -> dict:
        async with self._lock:
            for attempt in (0, 1):
                client = await self._ensure()
                try:
                    url, table, actions = await self._table(client, text_chars)
                except CdpError as exc:
                    if exc.code == "CDP_CLOSED":
                        await self._drop_client()
                        if attempt == 0:
                            continue
                    raise
                return {"url": url, "table": table, "actions": actions}

    async def act(
        self, steps: list[Step], snapshot: bool = True, text_chars: int = 600
    ) -> dict:
        async with self._lock:
            client = await self._ensure()
            results = []
            completed = 0
            closed = False
            for step in steps:
                try:
                    res = await self._perform(client, step, self._allowed_hosts)
                except CdpError as exc:
                    res = StepResult(step.op, False, exc.code, exc.message)
                    if exc.code == "CDP_TIMEOUT":
                        closed = True  # page state unknown: drop the client
                except Exception as exc:  # a bug, not the page: state unknown
                    res = StepResult(step.op, False, "INTERNAL", str(exc))
                    closed = True
                results.append(dataclasses.asdict(res))
                if not res.ok:
                    closed = closed or res.error == "CDP_CLOSED"
                    break
                completed += 1
            closed = closed or client.closed
            if closed:
                await self._drop_client()
            out = {"results": results, "completed": completed,
                   "url": self._last_url, "table": None}
            if snapshot and not closed:
                try:
                    out["url"], out["table"], _ = await self._table(client, text_chars)
                except CdpError as exc:
                    out["table_error"] = exc.code
                    if exc.code in ("CDP_CLOSED", "CDP_TIMEOUT"):
                        await self._drop_client()
            return out


_session: BrowserSession | None = None


def get_browser_session() -> BrowserSession:
    global _session
    if _session is None:
        _session = BrowserSession(
            config.FLOW_CDP_PORT,
            config.FLOW_CDP_ALLOWED_HOSTS,
            config.FLOW_PROJECT_ID,
            config.FLOW_CDP_TIMEOUT_S,
            ws_port=config.WS_PORT,
        )
    return _session
