"""iTerm2 entry point: starts the local panel server, registers it as a
toolbelt webview, and watches sessions for failed commands.

Run by iTerm2 (Scripts > AutoLaunch, via the launcher install.sh writes), or
directly for development:  .venv/bin/python -m intelligent_iterm.main
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets

import iterm2

from . import config
from .dock import Dock
from .panel import Panel
from .terminal import ItermTerminal
from .web import EventHub, WebServer, theme_vars

TOOL_ID = "com.intelligent-iterm.panel"
LOG_FILE = os.path.expanduser("~/Library/Logs/intelligent-iterm.log")
# Restarting the script from the Scripts menu starts a new copy without stopping
# the old one; two copies would both react to the hotkey and button. The newest
# copy owns this file, and older copies exit when they see they've been replaced.
PID_FILE = os.path.expanduser("~/Library/Caches/intelligent-iterm.pid")
log = logging.getLogger("intelligent_iterm")


def _owner_pid() -> int:
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


async def _exit_when_orphaned(app: iterm2.App, panel: Panel) -> None:
    """Exit when a newer copy takes over, or when iTerm2 goes away (quit/crash):
    the iterm2 library doesn't end the script on a lost connection by itself,
    so without this the web server and agent would linger after iTerm2 quits."""
    ticks = 0
    while True:
        await asyncio.sleep(1.5)
        ticks += 1
        reason = None
        if _owner_pid() not in (0, os.getpid()):
            reason = f"a newer copy (pid {_owner_pid()}) took over"
        elif ticks % 4 == 0:  # every ~6s: is iTerm2 still answering?
            try:
                await asyncio.wait_for(app.async_get_variable("effectiveTheme"), 10)
            except Exception as e:  # noqa: BLE001
                reason = f"lost connection to iTerm2 ({e!r})"
        if reason:
            log.info("%s; exiting", reason)
            await panel.shutdown()
            os._exit(0)  # drops the iTerm2 connection, releasing hotkey/button/tool


async def _retry_while_duplicate(register, what: str) -> None:
    """An older copy may still hold a registration for a moment while it exits."""
    for attempt in range(10):
        try:
            return await register()
        except Exception as e:  # noqa: BLE001
            if "DUPLICATE" not in repr(e) or attempt == 9:
                log.error("could not register %s: %r", what, e)
                return
            await asyncio.sleep(1)


async def _prune_closed_tabs(connection: iterm2.Connection, terminal: ItermTerminal, panel: Panel) -> None:
    """Each tab has its own conversation; when a tab closes, stop its agent."""
    async with iterm2.LayoutChangeMonitor(connection) as mon:
        while True:
            await mon.async_get()
            await asyncio.sleep(0.5)  # let the app object take in the new layout first
            try:
                await panel.prune(terminal.tab_ids())
            except Exception:  # noqa: BLE001 - keep watching
                log.exception("pruning conversations failed")


async def main(connection: iterm2.Connection) -> None:
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    app = await iterm2.async_get_app(connection)
    hub = EventHub()
    panel_ref: list = []
    terminal = ItermTerminal(connection, app, lambda f: panel_ref[0].command_failed(f))

    command, env = config.agent_command_and_env()
    panel = Panel(hub, terminal, command, env)
    panel_ref.append(panel)

    routes = panel.routes()
    web = WebServer(secrets.token_urlsafe(24), hub, routes, mcp=panel.mcp)
    port = await web.start(int(os.environ.get("INTELLIGENT_ITERM_PORT", "0")))
    panel.mcp_endpoint = (web.mcp_url, web.token)  # the agent's propose_command / read_terminal tools
    log.info("panel server on 127.0.0.1:%s", port)

    # Two ways to show the same panel: the toolbelt (right side) and a
    # bottom-docked browser split toggled with Ctrl+Shift+.
    await iterm2.async_register_web_view_tool(
        connection, "Claude", TOOL_ID, False, web.url)
    log.info("registered toolbelt tool %s", TOOL_ID)
    dock = Dock(connection, app, terminal, web.url, page_ready=hub.wait_pinned)

    async def toggle_dock(_body: dict) -> dict:
        return await dock.toggle()
    routes["/api/dock"] = toggle_dock

    asyncio.ensure_future(_exit_when_orphaned(app, panel))
    await _retry_while_duplicate(dock.register_status_bar_button, "status bar button")
    asyncio.ensure_future(dock.watch_hotkey())
    publish_theme = lambda theme: hub.set_sticky("theme", {"type": "theme", "vars": theme_vars(theme)})  # noqa: E731
    try:  # have the theme before any panel page loads, so it never paints in default colors
        theme = await terminal.get_theme()
        if theme:
            publish_theme(theme)
    except Exception:  # noqa: BLE001
        log.info("initial theme read failed", exc_info=True)
    try:
        await dock.replace_restored_panels()
    except Exception:  # noqa: BLE001 - never block startup on this
        log.exception("could not replace restored panels")
    asyncio.ensure_future(dock.watch_closed_panels())
    asyncio.ensure_future(terminal.watch_focus(publish_theme, panel.follow_active))
    asyncio.ensure_future(_prune_closed_tabs(connection, terminal, panel))
    try:
        await terminal.watch()
        await asyncio.Event().wait()  # keep running; iTerm2 stops us on quit
    finally:
        await panel.shutdown()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )
    iterm2.run_forever(main)


if __name__ == "__main__":
    run()
