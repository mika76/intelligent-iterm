"""End-to-end test of web server + panel + ACP client, with a fake agent and
fake terminal. Run: python3 -m unittest discover tests"""
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from intelligent_iterm.panel import Panel  # noqa: E402
from intelligent_iterm.web import EventHub, WebServer, theme_vars  # noqa: E402

FAKE_AGENT = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_agent.py")]


class FakeTerminal:
    def __init__(self):
        self.inserted = []
        self.failure = {"id": "p1", "command": "make", "exit": 2, "cwd": "/tmp",
                        "output": "make: *** No targets."}

    async def get_context(self, include_screen):
        ctx = {"cwd": "/tmp", "last": self.failure}
        if include_screen:
            ctx["screen"] = "SCREEN TEXT"
        return ctx

    def get_failure(self, fid):
        return self.failure if fid == "p1" else None

    async def insert(self, text, session_id=None, run=False):
        self.inserted.append(text)
        self.ran = run


async def http(port, method, path, body=None, token=None, host="127.0.0.1"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    data = json.dumps(body or {}).encode()
    hdr = f"{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Length: {len(data)}\r\n"
    if token:
        hdr += f"X-Token: {token}\r\n"
    w.write(hdr.encode() + b"\r\n" + data)
    raw = await r.read()
    w.close()
    await w.wait_closed()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), payload


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = EventHub()
        self.term = FakeTerminal()
        self.panel = Panel(self.hub, self.term, FAKE_AGENT, dict(os.environ))
        self.web = WebServer("secret", self.hub, self.panel.routes(), mcp=self.panel.mcp)
        self.port = await self.web.start()
        self.panel.mcp_endpoint = (self.web.mcp_url, "secret")
        self.queue, _ = self.hub.subscribe()

    async def asyncTearDown(self):
        await self.panel.shutdown()
        await self.web.stop()

    async def events_until(self, kind):
        seen = []
        while True:
            ev = await asyncio.wait_for(self.queue.get(), 10)
            seen.append(ev)
            if ev["type"] == kind:
                return seen

    async def test_propose_command_tool(self):
        status, _ = await http(self.port, "POST", "/api/send", {"text": "propose a fix"}, token="secret")
        self.assertEqual(status, 200)
        prop = (await self.events_until("proposal"))[-1]
        self.assertEqual((prop["command"], prop["reason"], prop["risks"]), ("ls -la", "list files", []))
        status, _ = await http(self.port, "POST", "/api/proposal", {"id": prop["id"], "action": "insert"},
                               token="secret")
        self.assertEqual(status, 200)
        seen = await self.events_until("turn_end")
        self.assertEqual(self.term.inserted, ["ls -la"])
        self.assertFalse(self.term.ran)
        self.assertTrue(any(e["type"] == "proposal_done" and e["outcome"] == "inserted" for e in seen))
        self.assertIn("inserted the command", "".join(e.get("text", "") for e in seen if e["type"] == "chunk"))

    async def test_mcp_needs_token_and_lists_tools(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        status, _ = await http(self.port, "POST", "/mcp", req)
        self.assertEqual(status, 403)
        status, body = await http(self.port, "POST", "/mcp", req, token="secret")
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(names, ["propose_command", "read_terminal"])
        status, _ = await http(self.port, "POST", "/mcp",
                               {"jsonrpc": "2.0", "method": "notifications/initialized"}, token="secret")
        self.assertEqual(status, 202)
        status, body = await http(self.port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "propose_command", "arguments": {"command": "a\rb", "reason": ""}}},
                                  token="secret")
        self.assertTrue(json.loads(body)["result"]["isError"])

    async def test_doctor_reports_every_check(self):
        status, _ = await http(self.port, "POST", "/api/doctor", {}, token="secret")
        self.assertEqual(status, 200)
        ev = (await self.events_until("doctor"))[-1]
        names = [c["name"] for c in ev["checks"]]
        self.assertIn("Node.js", names)
        self.assertIn("Claude Code login", names)
        self.assertFalse(ev["problemsOnly"])

    async def test_page_served_and_token_required(self):
        status, body = await http(self.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body)
        status, _ = await http(self.port, "POST", "/api/send", {"text": "hi"})
        self.assertEqual(status, 403)
        status, _ = await http(self.port, "POST", "/api/send", {"text": "hi"}, token="wrong")
        self.assertEqual(status, 403)
        status, _ = await http(self.port, "GET", "/", host="evil.example")
        self.assertEqual(status, 403)

    async def test_chat_streams_with_context(self):
        status, _ = await http(self.port, "POST", "/api/send",
                               {"text": "hello", "attach": True, "screen": True}, token="secret")
        self.assertEqual(status, 200)
        evs = await self.events_until("turn_end")
        text = "".join(e["text"] for e in evs if e["type"] == "chunk")
        self.assertIn("<terminal_context>", text)
        self.assertIn("make: *** No targets.", text)
        self.assertIn("SCREEN TEXT", text)
        self.assertTrue(text.rstrip().endswith("hello"))
        self.assertEqual(evs[-1]["stopReason"], "end_turn")

    async def test_explain_failed_command(self):
        self.panel.command_failed(self.term.failure)
        await http(self.port, "POST", "/api/explain", {"id": "p1"}, token="secret")
        evs = await self.events_until("turn_end")
        text = "".join(e["text"] for e in evs if e["type"] == "chunk")
        self.assertIn("The command in the terminal context failed", text)
        self.assertIn("untrusted data", text)
        self.assertEqual([e for e in evs if e["type"] not in ("agent", "commands")][0]["type"], "failed_command")
        self.assertIn({"type": "failure_handled", "id": "p1"}, evs)

    async def test_permission_round_trip(self):
        await http(self.port, "POST", "/api/send", {"text": "tool please"}, token="secret")
        evs = await self.events_until("permission")
        await http(self.port, "POST", "/api/permission",
                   {"id": evs[-1]["id"], "optionId": "reject"}, token="secret")
        evs = await self.events_until("turn_end")
        text = "".join(e["text"] for e in evs if e["type"] == "chunk")
        self.assertIn('"optionId": "reject"', text)

    async def test_theme_baked_into_first_paint(self):
        self.hub.set_sticky("theme", {"type": "theme", "vars": theme_vars(
            {"bg": "#15191f", "fg": "#dcdcdc", "font": "Fira Code", "size": 17})})
        status, body = await http(self.port, "GET", "/")
        self.assertEqual(status, 200)
        head = body.split(b"<head>")[0].decode()
        self.assertIn('data-theme="iterm"', head)
        self.assertIn("--bg:#15191f", head)
        self.assertIn("color-scheme:dark", head)
        self.assertIn("--size:14px", head)
        self.assertIn("&#x27;Fira Code&#x27;", head)  # quoted safely inside the attribute

    async def test_insert_goes_to_terminal(self):
        await http(self.port, "POST", "/api/insert", {"text": "ls -la"}, token="secret")
        self.assertEqual(self.term.inserted, ["ls -la"])

    async def test_open_link_only_http(self):
        for url in ["file:///etc/passwd", "javascript:alert(1)", "-a Calculator", "https://x\nrm"]:
            status, body = await http(self.port, "POST", "/api/open", {"url": url}, token="secret")
            self.assertFalse(json.loads(body)["ok"], url)


if __name__ == "__main__":
    unittest.main()
