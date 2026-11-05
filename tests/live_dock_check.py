"""Manual live check of the bottom dock against a running iTerm2. Uses the fake
agent; opens its own window and closes it at the end.

    .venv/bin/python tests/live_dock_check.py
"""
import asyncio
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import iterm2  # noqa: E402

from intelligent_iterm.dock import Dock  # noqa: E402
from intelligent_iterm.panel import Panel  # noqa: E402
from intelligent_iterm.terminal import ItermTerminal  # noqa: E402
from intelligent_iterm.web import EventHub, WebServer  # noqa: E402
from test_bridge import FAKE_AGENT  # noqa: E402

MARK = "intelligent-iterm-dock-check"


async def main(connection):
    app = await iterm2.async_get_app(connection)
    hub = EventHub()
    term = ItermTerminal(connection, app, lambda f: None)
    panel = Panel(hub, term, FAKE_AGENT, dict(os.environ))
    web = WebServer("dockcheck", hub, panel.routes())
    await web.start()
    dock = Dock(connection, app, term, web.url)
    focus_task = asyncio.ensure_future(term.watch_focus(lambda t: None))

    window = await iterm2.Window.async_create(connection)
    shell = window.current_tab.current_session
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name, "" if cond else detail)

    try:
        await shell.async_activate()
        await asyncio.sleep(1.5)
        before = shell.grid_size
        check("dock opens", (await dock.toggle())["state"] == "open")
        tab = window.current_tab
        panel_id = dock._panels.get(tab.tab_id)
        panel_session = app.get_session_by_id(panel_id)
        check("panel is a split in the same tab", panel_session is not None and len(tab.sessions) == 2)
        props = (await panel_session.async_get_profile()).all_properties
        check("panel is a browser session", props.get("Custom Command") == "Browser", props.get("Custom Command"))

        connected = False
        for _ in range(60):  # the page opens /events once it has loaded
            if hub._clients:
                connected = True
                break
            await asyncio.sleep(0.25)
        check("panel page loaded and connected", connected)
        check("panel page title", (await panel_session.async_get_variable("session.name") or "").startswith("Claude"),
              await panel_session.async_get_variable("session.name"))
        await asyncio.sleep(0.5)
        check("terminal shrank above the panel",
              shell.grid_size.height < before.height, (before.height, shell.grid_size.height))

        await panel_session.async_activate()
        await asyncio.sleep(0.5)
        active = term.active_session()
        if "-v" in sys.argv:
            ctw = app.current_terminal_window
            print("  shell", shell.session_id, "panel", panel_id, "active", active and active.session_id)
            print("  tab sessions", [s.session_id for s in tab.sessions], "frontmost window",
                  ctw and ctw.window_id, "test window", window.window_id,
                  "current", ctw and ctw.current_tab.current_session.session_id)
        check("focus in panel -> context targets terminal",
              active is not None and active.session_id == shell.session_id,
              active.session_id if active else None)

        await term.insert(f"echo {MARK}")
        await asyncio.sleep(0.7)
        screen = await shell.async_get_screen_contents()
        rows = [screen.line(i).string for i in range(screen.number_of_lines)]
        check("insert lands in terminal, not run", any(f"echo {MARK}" in r for r in rows)
              and not any(r.strip() == MARK for r in rows))
        check("insert refocuses terminal",
              app.current_terminal_window is None
              or app.current_terminal_window.current_tab.current_session.session_id == shell.session_id)

        check("dock closes", (await dock.toggle())["state"] == "closed")
        await asyncio.sleep(0.5)
        check("panel session gone", app.get_session_by_id(panel_id) is None and len(tab.sessions) == 1)
    finally:
        focus_task.cancel()
        await shell.async_send_text("\x15")
        await window.async_close(force=True)
        await panel.shutdown()
        await web.stop()
    print("ALL PASS" if ok else "SOME CHECKS FAILED")


iterm2.run_until_complete(main)
