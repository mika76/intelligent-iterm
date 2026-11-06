"""Header pickers: model, mode and effort, applied to every conversation and remembered."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from intelligent_iterm.config import AgentSpec  # noqa: E402
from intelligent_iterm.panel import Panel  # noqa: E402
from intelligent_iterm.settings import Settings  # noqa: E402
from intelligent_iterm.web import EventHub, WebServer  # noqa: E402
from test_bridge import FAKE_AGENT, http  # noqa: E402
from test_tabs import TabTerminal  # noqa: E402


def pickers(hub, conv="A"):
    return {o["id"]: o for o in hub.get_conv_sticky(conv, "agent")["options"]}


class ConfigTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"
        await self.start()

    async def start(self):
        self.hub = EventHub()
        self.panel = Panel(self.hub, TabTerminal(), FAKE_AGENT, dict(os.environ), settings=Settings(self.path))
        self.web = WebServer("secret", self.hub, self.panel.routes(), mcp=self.panel.mcp)
        self.port = await self.web.start()

    async def stop(self):
        await self.panel.shutdown()
        await self.web.stop()

    async def asyncTearDown(self):
        await self.stop()
        self.dir.cleanup()

    async def post(self, path, body):
        status, payload = await http(self.port, "POST", path, body, token="secret")
        return status, payload

    async def test_pickers_offer_model_mode_effort_without_bypass(self):
        await self.panel.conversation("A").ensure_agent()
        p = pickers(self.hub)
        self.assertEqual(list(p), ["model", "mode", "effort"])  # not fast mode
        self.assertEqual(p["mode"]["current"], "default")  # the panel starts in Manual
        self.assertNotIn("bypassPermissions", [c["value"] for c in p["mode"]["choices"]])
        self.assertIn({"value": "default", "name": "Default model"}, p["model"]["choices"])
        self.assertIn({"value": "low", "name": "Low effort"}, p["effort"]["choices"])

    async def test_change_applies_to_open_and_new_conversations_and_persists(self):
        a = self.panel.conversation("A")
        await a.ensure_agent()
        status, _ = await self.post("/api/config", {"configId": "model", "value": "haiku"})
        self.assertEqual(status, 200)
        self.assertEqual(pickers(self.hub)["model"]["current"], "haiku")
        opts = {o["id"]: o["currentValue"] for o in a.agent.session_info["configOptions"]}
        self.assertEqual(opts["model"], "haiku")
        await self.post("/api/config", {"configId": "mode", "value": "acceptEdits"})
        # A new tab picks up both.
        b = self.panel.conversation("B")
        await b.ensure_agent()
        opts = {o["id"]: o["currentValue"] for o in b.agent.session_info["configOptions"]}
        self.assertEqual((opts["model"], opts["mode"]), ("haiku", "acceptEdits"))
        self.assertEqual(pickers(self.hub, "B")["model"]["current"], "haiku")
        # And so does the next run.
        await self.stop()
        await self.start()
        c = self.panel.conversation("A")
        await c.ensure_agent()
        self.assertEqual(pickers(self.hub)["model"]["current"], "haiku")
        self.assertEqual(pickers(self.hub)["mode"]["current"], "acceptEdits")

    async def test_rejects_unknown_and_hidden_choices(self):
        await self.panel.conversation("A").ensure_agent()
        for body in ({"configId": "mode", "value": "bypassPermissions"},
                     {"configId": "fast", "value": "on"},
                     {"configId": "model", "value": "gpt"}):
            status, _ = await self.post("/api/config", body)
            self.assertEqual(status, 500, body)
        self.assertFalse(self.path.exists())



class AgentTest(unittest.IsolatedAsyncioTestCase):
    """Two agents (both the fake one): Claude Code, and another without a system prompt."""

    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"
        await self.start()

    async def start(self):
        env = dict(os.environ)
        self.hub = EventHub()
        self.panel = Panel(self.hub, TabTerminal(), [], {}, settings=Settings(self.path), agents=[
            AgentSpec("claude", "Claude Code", FAKE_AGENT, env, claude=True),
            AgentSpec("other", "Other Agent", FAKE_AGENT, env)])
        self.web = WebServer("secret", self.hub, self.panel.routes(), mcp=self.panel.mcp)
        self.port = await self.web.start()

    async def asyncTearDown(self):
        await self.panel.shutdown()
        await self.web.stop()
        self.dir.cleanup()

    async def post(self, path, body):
        status, _ = await http(self.port, "POST", path, body, token="secret")
        return status

    def header(self, conv):
        return self.hub.get_conv_sticky(conv, "agent")

    async def turn(self, conv, text):
        q, _ = self.hub.subscribe(conv)
        self.assertEqual(await self.post("/api/send", {"text": text, "conv": conv}), 200)
        evs = []
        while not evs or evs[-1]["type"] != "turn_end":
            evs.append(await asyncio.wait_for(q.get(), 10))
        self.hub.unsubscribe(q)
        return "".join(e.get("text", "") for e in evs if e["type"] == "chunk")

    async def test_switching_agent_restarts_this_tab_only(self):
        await self.turn("A", "hello")
        await self.turn("B", "stays")
        h = self.header("A")
        self.assertEqual((h["id"], h["name"]), ("claude", "Claude Code"))
        self.assertEqual([a["id"] for a in h["agents"]], ["claude", "other"])
        self.assertEqual(await self.post("/api/agent", {"agent": "other", "conv": "A"}), 200)
        self.assertEqual(self.header("A")["id"], "other")
        _, replay = self.hub.subscribe("A")
        self.assertNotIn("hello", str([e for e in replay if e["type"] != "agent"]))
        self.assertEqual(self.header("B")["id"], "claude")  # other tabs keep theirs
        self.assertIn("stays", str(self.hub.subscribe("B")[1]))
        # New tabs use the agent picked last, also after a restart.
        self.assertEqual(self.panel.conversation("C").spec.id, "other")
        await self.panel.shutdown()
        await self.web.stop()
        await self.start()
        self.assertEqual(self.panel.conversation("A").spec.id, "other")

    async def test_agent_without_system_prompt_gets_instructions_once(self):
        self.panel.conversation("B")  # opened before the switch: stays on Claude Code
        await self.post("/api/agent", {"agent": "other", "conv": "A"})
        first = await self.turn("A", "one")
        second = await self.turn("A", "two")
        self.assertIn("<panel_instructions>", first)
        self.assertIn("propose_command", first)
        self.assertNotIn("<panel_instructions>", second)
        self.assertNotIn("<panel_instructions>", await self.turn("B", "claude tab"))

    async def test_picks_are_per_agent(self):
        await self.panel.conversation("A").ensure_agent()
        await self.post("/api/agent", {"agent": "other", "conv": "B"})
        await self.panel.conversation("B").ensure_agent()
        self.assertEqual(await self.post("/api/config", {"configId": "model", "value": "haiku", "conv": "B"}), 200)
        model = lambda conv: {o["id"]: o for o in self.header(conv)["options"]}["model"]["current"]  # noqa: E731
        self.assertEqual(model("B"), "haiku")
        self.assertEqual(model("A"), "default")
        self.assertEqual(self.panel.settings.option("other", "model"), "haiku")
        self.assertIsNone(self.panel.settings.option("claude", "model"))

    async def test_wrapped_panel_tool_call_is_taken_back(self):
        """OpenCode reports an MCP call as a generic "execute" tool and only names
        the tool in a later update."""
        conv = self.panel.conversation("A")
        q, _ = self.hub.subscribe("A")
        call = {"sessionUpdate": "tool_call", "toolCallId": "t9", "title": "execute", "rawInput": {}}
        await conv.on_update(call)
        await conv.on_update(dict(call, sessionUpdate="tool_call_update", status="in_progress",
                                  rawInput={"code": "await tools.iterm.propose_command({command: 'pwd'})"}))
        await conv.on_update(dict(call, sessionUpdate="tool_call_update", status="completed", rawInput=None))
        evs = [q.get_nowait() for _ in range(q.qsize())]
        self.assertEqual([e["type"] for e in evs], ["tool", "tool_hidden"])

    def test_duplicate_choices_shown_once(self):
        from intelligent_iterm.panel import picker_options
        opts = [{"id": "model", "type": "select", "category": "model", "currentValue": "auto", "options": [
            {"value": "auto", "name": "Auto"}, {"value": "auto", "name": "Auto"}, {"value": "x", "name": "X"}]}]
        self.assertEqual([c["value"] for c in picker_options(opts)[0]["choices"]], ["auto", "x"])

    async def test_unknown_agent_refused(self):
        self.assertEqual(await self.post("/api/agent", {"agent": "nope", "conv": "A"}), 500)


if __name__ == "__main__":
    unittest.main()
