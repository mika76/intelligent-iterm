"""Manual live check of per-tab conversations against a running iTerm2: the
toolbelt follows the active tab, a failure lands in its own tab's conversation,
and closing a tab drops its conversation. Uses the fake agent; opens its own
window and closes it at the end.

    .venv/bin/python tests/live_tabs_check.py
"""
import asyncio
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import iterm2  # noqa: E402

from intelligent_iterm.panel import Panel  # noqa: E402
from intelligent_iterm.terminal import ItermTerminal  # noqa: E402
from intelligent_iterm.web import EventHub  # noqa: E402
from test_bridge import FAKE_AGENT  # noqa: E402


async def main(connection):
    app = await iterm2.async_get_app(connection)
    hub = EventHub()
    panel_ref = []
    term = ItermTerminal(connection, app, lambda f: panel_ref[0].command_failed(f))
    panel = Panel(hub, term, FAKE_AGENT, dict(os.environ))
    panel_ref.append(panel)
    window = await iterm2.Window.async_create(connection)
    tab_a = window.current_tab
    tab_b = await window.async_create_tab()
    tasks = [asyncio.ensure_future(term.watch_focus(lambda t: None, panel.follow_active)),
             asyncio.ensure_future(term.watch())]
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name, "" if cond else detail)

    async def switches(q, wait=1.5):
        await asyncio.sleep(wait)
        return [e["conv"] for e in [q.get_nowait() for _ in range(q.qsize())] if e["type"] == "switch"]

    try:
        await tab_a.async_activate()
        await asyncio.sleep(1.0)
        follower, backlog = hub.subscribe()
        check("toolbelt starts on the active tab", backlog[0]["conv"] == tab_a.tab_id,
              (backlog[0], tab_a.tab_id))
        await tab_b.async_activate()
        check("toolbelt follows to tab B", tab_b.tab_id in await switches(follower))
        await tab_a.async_activate()
        check("toolbelt follows back to tab A", tab_a.tab_id in await switches(follower))

        pinned_b, _ = hub.subscribe(tab_b.tab_id)
        await asyncio.sleep(1.0)  # let shell integration settle in tab B
        await tab_b.current_session.async_send_text("false\n")  # fails while tab A is active
        failed = None
        for _ in range(20):
            evs = [pinned_b.get_nowait() for _ in range(pinned_b.qsize())]
            failed = next((e for e in evs if e["type"] == "failed_command"), failed)
            if failed:
                break
            await asyncio.sleep(0.25)
        check("failure in a background tab reaches that tab", failed is not None)
        _, replay_a = hub.subscribe(tab_a.tab_id)
        check("…and not the active tab", not any(e["type"] == "failed_command" for e in replay_a))

        await tab_b.async_close(force=True)
        for _ in range(20):
            if tab_b.tab_id not in hub.conversations():
                break
            await panel.prune(term.tab_ids())
            await asyncio.sleep(0.25)
        check("closed tab's conversation is dropped", tab_b.tab_id not in hub.conversations(),
              hub.conversations())
    finally:
        for t in tasks:
            t.cancel()
        try:
            await window.async_close(force=True)
        except Exception:  # noqa: BLE001
            pass
        await panel.shutdown()
    print("ALL PASS" if ok else "SOME CHECKS FAILED")


iterm2.run_until_complete(main)
