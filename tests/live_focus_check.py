"""Manual live check: in iTerm2's docked panel (WebKit), typing while the focus is
off the input (here: on the Send button) still lands in the input, and a waiting
command card can be answered with Up, Right and Enter. Presses keys
through System Events, so iTerm2 must stay frontmost while it runs; opens its own
window and closes it at the end.

    .venv/bin/python tests/live_focus_check.py
"""
import asyncio, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..")); sys.path.insert(0, HERE)
import iterm2
from intelligent_iterm.dock import Dock
from intelligent_iterm.panel import Panel
from intelligent_iterm.terminal import ItermTerminal
from intelligent_iterm.web import EventHub, WebServer
from test_bridge import FAKE_AGENT

def keys(script):
    r = subprocess.run(["osascript", "-e", f'tell application "System Events" to tell process "iTerm2"\nif frontmost is false then return "background"\n{script}\nreturn "ok"\nend tell'],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()

async def main(c):
    app = await iterm2.async_get_app(c)
    hub = EventHub()
    term = ItermTerminal(c, app, lambda f: None)
    panel = Panel(hub, term, FAKE_AGENT, dict(os.environ))
    web = WebServer("fc", hub, panel.routes(), mcp=panel.mcp)
    await web.start(); panel.mcp_endpoint = (web.mcp_url, web.token)
    dock = Dock(c, app, term, web.url)
    w = await iterm2.Window.async_create(c)
    shell = w.current_tab.current_session
    tab = w.current_tab.tab_id
    try:
        await app.async_activate(); await shell.async_activate(); await asyncio.sleep(1.5)
        await dock.toggle(shell)
        for _ in range(60):
            if hub._clients: break
            await asyncio.sleep(0.25)
        await asyncio.sleep(1.5)
        q, _ = hub.subscribe(tab)
        ps = app.get_session_by_id(dock._panels[tab])
        await app.async_activate(); await ps.async_activate(); await asyncio.sleep(0.8)
        print("tab to Send button:", keys('key code 48'))  # Tab: focus leaves the textarea
        await asyncio.sleep(0.3)
        print("type:", keys('keystroke "hello panel"'))
        await asyncio.sleep(0.3)
        print("enter:", keys('key code 36'))
        got = None
        for _ in range(20):
            while not q.empty():
                ev = q.get_nowait()
                if ev["type"] == "user": got = ev["text"]
            if got: break
            await asyncio.sleep(0.25)
        print("RESULT:", repr(got), "PASS" if got == "hello panel" else "FAIL", flush=True)

        # Keys on a waiting card: Up from the empty input, Right to Run, Enter.
        await asyncio.sleep(1.0)
        await panel.api_send({"conv": tab, "text": "propose something"})
        for _ in range(40):
            if panel.conversation(tab).proposals: break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        await app.async_activate(); await ps.async_activate(); await asyncio.sleep(0.5)
        print("keys:", keys('key code 126'), keys('key code 124'), keys('key code 36'))  # Up, Right, Return
        outcome = None
        for _ in range(20):
            while not q.empty():
                ev = q.get_nowait()
                if ev["type"] == "proposal_done": outcome = ev["outcome"]
            if outcome: break
            await asyncio.sleep(0.25)
        # "ran" only comes after the command was typed into the terminal (insert raises otherwise).
        print("CARD:", repr(outcome), "PASS" if outcome == "ran" else "FAIL", flush=True)
    finally:
        await w.async_close(force=True)
        await panel.shutdown(); await web.stop()
iterm2.run_until_complete(main)
