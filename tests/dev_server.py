"""Run the panel in a normal browser without iTerm2, for UI work.

    python3 tests/dev_server.py            # fake agent (free, instant)
    python3 tests/dev_server.py --real     # real Claude Code via claude-agent-acp

Uses a fake terminal; "Insert" just prints to stdout.
"""
import asyncio
import logging
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))
from intelligent_iterm import config  # noqa: E402
from intelligent_iterm.panel import Panel  # noqa: E402
from intelligent_iterm.web import EventHub, WebServer  # noqa: E402
from test_bridge import FAKE_AGENT, FakeTerminal  # noqa: E402


class PrintingTerminal(FakeTerminal):
    async def insert(self, text):
        print("INSERT:", repr(text), flush=True)


async def run():
    hub = EventHub()
    term = PrintingTerminal()
    if "--real" in sys.argv:
        command, env = config.agent_command_and_env()
    else:
        command, env = FAKE_AGENT, dict(os.environ)
    panel = Panel(hub, term, command, env)
    web = WebServer("dev", hub, panel.routes())
    await web.start(int(os.environ.get("PORT", "8765")))
    print("Panel at", web.url, flush=True)
    panel.command_failed(term.failure)
    await asyncio.Event().wait()


logging.basicConfig(level=logging.INFO)
asyncio.run(run())
