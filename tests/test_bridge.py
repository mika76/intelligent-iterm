"""End-to-end test of web server + panel + ACP client, with a fake agent and
fake terminal. Run: python3 -m unittest discover tests"""
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from intelligent_iterm.panel import Panel  # noqa: E402
from intelligent_iterm.web import EventHub, WebServer  # noqa: E402

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

    async def insert(self, text):
        self.inserted.append(text)


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
        self.web = WebServer("secret", self.hub, self.panel.routes())
        self.port = await self.web.start()
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
        self.assertIn("Explain why `make` failed (exit 2)", text)
        self.assertEqual(evs[0]["type"], "failed_command")

    async def test_permission_round_trip(self):
        await http(self.port, "POST", "/api/send", {"text": "use a tool"}, token="secret")
        evs = await self.events_until("permission")
        await http(self.port, "POST", "/api/permission",
                   {"id": evs[-1]["id"], "optionId": "reject"}, token="secret")
        evs = await self.events_until("turn_end")
        text = "".join(e["text"] for e in evs if e["type"] == "chunk")
        self.assertIn('"optionId": "reject"', text)

    async def test_insert_goes_to_terminal(self):
        await http(self.port, "POST", "/api/insert", {"text": "ls -la"}, token="secret")
        self.assertEqual(self.term.inserted, ["ls -la"])


if __name__ == "__main__":
    unittest.main()
