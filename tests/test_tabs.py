"""One conversation per iTerm2 tab: separate histories and agents, the toolbelt
following the active tab, failures and proposals landing in the right tab."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from intelligent_iterm.panel import Panel  # noqa: E402
from intelligent_iterm.web import EventHub, WebServer  # noqa: E402
from test_bridge import FAKE_AGENT, FakeTerminal, http  # noqa: E402


class TabTerminal(FakeTerminal):
    """Two tabs, A and B, with one terminal session each."""

    def __init__(self):
        super().__init__()
        self.active = "A"
        self.tabs = {"sA": "A", "sB": "B"}

    def active_tab_id(self):
        return self.active

    def active_session_id(self):
        return {"A": "sA", "B": "sB"}[self.active]

    def tab_of(self, session_id):
        return self.tabs.get(session_id)


async def drain(q, until, timeout=10):
    seen = []
    while True:
        ev = await asyncio.wait_for(q.get(), timeout)
        seen.append(ev)
        if until(ev):
            return seen


class TabsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = EventHub()
        self.term = TabTerminal()
        self.panel = Panel(self.hub, self.term, FAKE_AGENT, dict(os.environ))
        self.web = WebServer("secret", self.hub, self.panel.routes(), mcp=self.panel.mcp)
        self.port = await self.web.start()
        self.panel.mcp_endpoint = (self.web.mcp_url, "secret")

    async def asyncTearDown(self):
        await self.panel.shutdown()
        await self.web.stop()

    async def send(self, conv, text):
        status, _ = await http(self.port, "POST", "/api/send", {"text": text, "conv": conv}, token="secret")
        self.assertEqual(status, 200)

    async def test_each_tab_has_its_own_history_and_agent(self):
        qa, backlog = self.hub.subscribe("A")
        self.assertEqual(backlog[0], {"type": "switch", "conv": "A"})
        qb, _ = self.hub.subscribe("B")
        await self.send("A", "hello from A")
        await drain(qa, lambda e: e["type"] == "turn_end")
        await self.send("B", "hello from B")
        seen_b = await drain(qb, lambda e: e["type"] == "turn_end")
        leaked = [e for e in [qa.get_nowait() for _ in range(qa.qsize())]
                  if e["type"] not in ("agent", "commands")]  # header info is shared by design
        self.assertEqual(leaked, [], "tab A's pane saw tab B's events")
        self.assertIn("hello from B", "".join(e.get("text", "") for e in seen_b))
        a, b = self.panel.conversation("A"), self.panel.conversation("B")
        self.assertIsNot(a.agent, b.agent)
        self.assertTrue(a.agent.alive and b.agent.alive)
        _, replay_a = self.hub.subscribe("A")
        self.assertNotIn("hello from B", str(replay_a))
        self.assertIn("hello from A", str(replay_a))

    async def test_toolbelt_follows_active_tab(self):
        await self.send("B", "only in B")
        await asyncio.sleep(0.3)
        follower, backlog = self.hub.subscribe()  # toolbelt
        self.assertEqual(backlog[0]["conv"], "A")
        self.term.active = "B"
        self.panel.follow_active()
        seen = await drain(follower, lambda e: e["type"] == "user")
        self.assertEqual(seen[0], {"type": "switch", "conv": "B"})
        self.assertEqual(seen[-1]["text"], "only in B")
        self.panel.follow_active()  # no change: no second switch
        self.assertTrue(all(e["type"] != "switch" for e in [follower.get_nowait() for _ in range(follower.qsize())]))

    async def test_failure_shows_in_its_own_tab(self):
        qa, _ = self.hub.subscribe("A")
        qb, _ = self.hub.subscribe("B")
        self.panel.command_failed({"id": "sB:1", "session_id": "sB", "command": "make", "exit": 2})
        evs = [qb.get_nowait() for _ in range(qb.qsize())]
        self.assertEqual([e["type"] for e in evs if e["type"] != "agent"], ["failed_command"])
        self.assertTrue(qa.empty())

    async def test_proposal_goes_to_the_calling_conversation(self):
        qb, _ = self.hub.subscribe("B")
        await self.send("B", "propose a fix")
        prop = (await drain(qb, lambda e: e["type"] == "proposal"))[-1]
        status, _ = await http(self.port, "POST", "/api/proposal",
                               {"id": prop["id"], "action": "insert", "conv": "B"}, token="secret")
        self.assertEqual(status, 200)
        await drain(qb, lambda e: e["type"] == "turn_end")
        self.assertEqual(self.term.inserted, ["ls -la"])

    async def test_new_chat_and_closed_tabs(self):
        qa, _ = self.hub.subscribe("A")
        await self.send("A", "first")
        await drain(qa, lambda e: e["type"] == "turn_end")
        await self.send("B", "other tab")
        await asyncio.sleep(0.3)
        status, _ = await http(self.port, "POST", "/api/new", {"conv": "A"}, token="secret")
        self.assertEqual(status, 200)
        self.assertEqual((await drain(qa, lambda e: e["type"] == "reset"))[-1]["type"], "reset")
        _, replay_b = self.hub.subscribe("B")
        self.assertIn("other tab", str(replay_b), "New chat in A cleared B")
        agent_b = self.panel.conversation("B").agent
        await self.panel.prune({"A"})  # tab B closed
        self.assertFalse(agent_b.alive)
        self.assertNotIn("B", self.hub.conversations())


if __name__ == "__main__":
    unittest.main()
