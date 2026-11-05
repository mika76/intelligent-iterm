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
log = logging.getLogger("intelligent_iterm")


async def main(connection: iterm2.Connection) -> None:
    app = await iterm2.async_get_app(connection)
    hub = EventHub()
    panel_ref: list = []
    terminal = ItermTerminal(connection, app, lambda f: panel_ref[0].command_failed(f))

    command, env = config.agent_command_and_env()
    panel = Panel(hub, terminal, command, env)
    panel_ref.append(panel)

    routes = panel.routes()
    web = WebServer(secrets.token_urlsafe(24), hub, routes)
    port = await web.start(int(os.environ.get("INTELLIGENT_ITERM_PORT", "0")))
    log.info("panel server on 127.0.0.1:%s", port)

    # Two ways to show the same panel: the toolbelt (right side) and a
    # bottom-docked browser split toggled with Ctrl+Shift+.
    await iterm2.async_register_web_view_tool(
        connection, "Claude", TOOL_ID, False, web.url)
    log.info("registered toolbelt tool %s", TOOL_ID)
    dock = Dock(connection, app, terminal, web.url)

    async def toggle_dock(_body: dict) -> dict:
        return await dock.toggle()
    routes["/api/dock"] = toggle_dock

    await dock.register_status_bar_button()
    asyncio.ensure_future(dock.watch_hotkey())
    asyncio.ensure_future(dock.watch_closed_panels())
    asyncio.ensure_future(terminal.watch_focus(
        lambda theme: hub.set_sticky("theme", {"type": "theme", "vars": theme_vars(theme)})))
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
