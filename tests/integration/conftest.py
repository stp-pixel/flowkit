"""Live-browser fixtures: a headless Chromium driven over CDP against local pages.

CI runs only tests/unit; these tests need a Chromium binary and skip without one.
"""
import asyncio
import functools
import glob
import http.server
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from agent.browser.cdp import CdpClient, list_pages

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "browser"
TIMEOUT_S = 10.0


def _find_chromium() -> str | None:
    env = os.environ.get("CHROME_PATH")
    if env and os.path.exists(env):
        return env
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    if os.path.exists("/opt/pw-browsers/chromium"):
        return "/opt/pw-browsers/chromium"
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return found[-1] if found else None


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


@pytest.fixture(scope="session")
def fixture_server():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def chromium(tmp_path_factory):
    exe = _find_chromium()
    if exe is None:
        pytest.skip("no Chromium")
    profile = tmp_path_factory.mktemp("chrome-profile")
    proc = subprocess.Popen(
        [
            exe,
            "--headless=new",
            "--no-sandbox",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1200,800",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + TIMEOUT_S
        port = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"Chromium exited early with code {proc.returncode}")
            try:
                first = port_file.read_text().splitlines()[0].strip()
                port = int(first)
                break
            except (OSError, IndexError, ValueError):
                time.sleep(0.05)
        if port is None:
            pytest.fail("Chromium did not write DevToolsActivePort within 10 s")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
async def cdp_page(chromium, fixture_server):
    """Factory: ``await cdp_page("flow_like.html")`` -> CdpClient on a fresh tab."""
    port = chromium
    async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
        resp = await http.get(f"http://127.0.0.1:{port}/json/version")
        resp.raise_for_status()
        browser_ws = resp.json()["webSocketDebuggerUrl"]
    browser = await CdpClient.connect(browser_ws, TIMEOUT_S)
    opened: list[tuple[str, CdpClient]] = []

    async def open_page(page_name: str) -> CdpClient:
        url = f"{fixture_server}/{page_name}"
        target_id = (await browser.send("Target.createTarget", {"url": url}))["targetId"]
        deadline = time.monotonic() + TIMEOUT_S
        target = None
        while target is None:
            target = next((p for p in await list_pages(port) if p.id == target_id), None)
            if target is None:
                if time.monotonic() > deadline:
                    raise AssertionError(f"target {target_id} never listed")
                await asyncio.sleep(0.05)
        client = await CdpClient.connect(target.ws_url, TIMEOUT_S)
        opened.append((target_id, client))
        ready = f"document.readyState === 'complete' && location.href === {url!r}"
        while not await client.evaluate(ready):
            if time.monotonic() > deadline:
                raise AssertionError(f"{url} did not finish loading")
            await asyncio.sleep(0.05)
        return client

    try:
        yield open_page
    finally:
        for target_id, client in opened:
            if not client.closed:
                await client.close()
            try:
                await browser.send("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass
        await browser.close()
