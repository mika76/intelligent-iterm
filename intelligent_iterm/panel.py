"""Panel logic: turns UI actions into ACP prompts and agent updates into UI events.

Independent of iTerm2 — the terminal is passed in as an object with
`get_context(include_screen)`, `insert(text)` and `get_failure(id)`, so this
can be tested with a fake.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
from typing import Any, Dict, List, Optional

from .acp import AcpClient
from .web import EventHub

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are running inside a side panel docked next to the user's iTerm2 terminal.
The user may attach terminal context (working directory, last command, its exit
status and output, or the visible screen) inside <terminal_context> tags.

Guidelines:
- Be concise; the panel is narrow.
- When you suggest a shell command for the user to run, put it alone in a
  ```bash fenced block (one command per block, single line where possible).
  The panel shows an "Insert" button that types it into the terminal without
  running it. Prefer suggesting commands over running them yourself.
- When explaining an error, give the likely cause first, then the fix.
"""

MAX_OUTPUT_CHARS = 12000


class Panel:
    def __init__(self, hub: EventHub, terminal: Any, agent_command: List[str],
                 agent_env: Dict[str, str]) -> None:
        self.hub = hub
        self.terminal = terminal
        self._agent_command = agent_command
        self._agent_env = agent_env
        self._agent: Optional[AcpClient] = None
        self._agent_lock = asyncio.Lock()
        self._turn: Optional[asyncio.Task] = None
        self._permissions: Dict[str, asyncio.Future] = {}
        self._perm_ids = itertools.count(1)

    def routes(self) -> Dict[str, Any]:
        return {
            "/api/send": self.api_send,
            "/api/explain": self.api_explain,
            "/api/insert": self.api_insert,
            "/api/permission": self.api_permission,
            "/api/cancel": self.api_cancel,
            "/api/new": self.api_new,
        }

    # ---- UI endpoints ----------------------------------------------------

    async def api_send(self, body: Dict[str, Any]) -> Dict[str, Any]:
        text = str(body.get("text", "")).strip()
        if not text:
            raise ValueError("empty message")
        blocks: List[Dict[str, Any]] = []
        label = None
        if body.get("attach") or body.get("screen"):
            ctx = await self.terminal.get_context(include_screen=bool(body.get("screen")))
            blocks.append({"type": "text", "text": format_context(ctx)})
            label = context_label(ctx)
        blocks.append({"type": "text", "text": text})
        return await self._start_turn(text, blocks, label)

    async def api_explain(self, body: Dict[str, Any]) -> Dict[str, Any]:
        failure = self.terminal.get_failure(str(body.get("id", "")))
        if failure is None:
            raise ValueError("that command is no longer available")
        ctx = {"cwd": failure.get("cwd"), "last": failure}
        text = f"Explain why `{failure['command']}` failed (exit {failure['exit']}) and how to fix it."
        blocks = [{"type": "text", "text": format_context(ctx)}, {"type": "text", "text": text}]
        return await self._start_turn(text, blocks, context_label(ctx))

    async def api_insert(self, body: Dict[str, Any]) -> Dict[str, Any]:
        await self.terminal.insert(str(body.get("text", "")))
        return {}

    async def api_permission(self, body: Dict[str, Any]) -> Dict[str, Any]:
        fut = self._permissions.pop(str(body.get("id")), None)
        if fut and not fut.done():
            option = body.get("optionId")
            fut.set_result({"outcome": "selected", "optionId": option} if option
                           else {"outcome": "cancelled"})
        return {}

    async def api_cancel(self, _body: Dict[str, Any]) -> Dict[str, Any]:
        self._cancel_permissions()
        if self._agent and self._turn and not self._turn.done():
            await self._agent.cancel()
        return {}

    async def api_new(self, _body: Dict[str, Any]) -> Dict[str, Any]:
        await self.api_cancel({})
        async with self._agent_lock:
            if self._agent:
                await self._agent.stop()
                self._agent = None
        self.hub.clear_history()
        self.hub.publish({"type": "reset"})
        return {}

    # ---- agent -------------------------------------------------------------

    async def _ensure_agent(self) -> AcpClient:
        async with self._agent_lock:
            if self._agent and self._agent.alive:
                return self._agent
            self.hub.publish({"type": "status", "text": "Starting Claude Code…"})
            agent = AcpClient(self._agent_command, self._agent_env,
                              self._on_update, self._on_permission)
            await agent.start()
            ctx = await self.terminal.get_context(include_screen=False)
            cwd = ctx.get("cwd") if ctx.get("cwd") and os.path.isdir(ctx["cwd"]) else os.path.expanduser("~")
            await agent.new_session(cwd, SYSTEM_PROMPT)
            self.hub.publish({"type": "status", "text": f"Session started in {cwd}"})
            self._agent = agent
            return agent

    async def _start_turn(self, text: str, blocks: List[Dict[str, Any]],
                          label: Optional[str]) -> Dict[str, Any]:
        if self._turn and not self._turn.done():
            raise RuntimeError("Claude is still answering; wait or press Stop")
        self.hub.publish({"type": "user", "text": text, "context": label})
        self._turn = asyncio.ensure_future(self._run_turn(blocks))
        return {}

    async def _run_turn(self, blocks: List[Dict[str, Any]]) -> None:
        try:
            agent = await self._ensure_agent()
            result = await agent.prompt(blocks)
            self.hub.publish({"type": "turn_end", "stopReason": result.get("stopReason")})
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed")
            self.hub.publish({"type": "error", "message": str(e)})
            self.hub.publish({"type": "turn_end", "stopReason": "error"})
        finally:
            self._cancel_permissions()

    async def _on_update(self, u: Dict[str, Any]) -> None:
        kind = u.get("sessionUpdate")
        content = u.get("content") or {}
        if kind == "agent_message_chunk" and content.get("type") == "text":
            self.hub.publish({"type": "chunk", "text": content["text"]})
        elif kind == "agent_thought_chunk":
            self.hub.publish({"type": "thinking"})
        elif kind in ("tool_call", "tool_call_update"):
            self.hub.publish({"type": "tool", "id": u.get("toolCallId"),
                              "title": u.get("title"), "status": u.get("status")})

    async def _on_permission(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pid = str(next(self._perm_ids))
        fut = asyncio.get_event_loop().create_future()
        self._permissions[pid] = fut
        tool = params.get("toolCall") or {}
        self.hub.publish({
            "type": "permission", "id": pid, "title": tool.get("title") or "Tool call",
            "options": [{"optionId": o["optionId"], "name": o["name"], "kind": o.get("kind")}
                        for o in params.get("options", [])],
        })
        try:
            return await fut
        finally:
            self.hub.publish({"type": "permission_done", "id": pid})

    def _cancel_permissions(self) -> None:
        for fut in self._permissions.values():
            if not fut.done():
                fut.set_result({"outcome": "cancelled"})
        self._permissions.clear()

    # ---- terminal → UI -----------------------------------------------------

    def command_failed(self, failure: Dict[str, Any]) -> None:
        """Called by the terminal watcher when a command exits non-zero."""
        self.hub.publish({"type": "failed_command", "id": failure["id"],
                          "command": failure["command"], "exit": failure["exit"],
                          "cwd": failure.get("cwd")})

    async def shutdown(self) -> None:
        if self._agent:
            await self._agent.stop()


def format_context(ctx: Dict[str, Any]) -> str:
    parts = ["<terminal_context>"]
    if ctx.get("cwd"):
        parts.append(f"cwd: {ctx['cwd']}")
    last = ctx.get("last")
    if last:
        parts.append(f"last command: {last.get('command')}")
        parts.append(f"exit status: {last.get('exit')}")
        out = (last.get("output") or "")[-MAX_OUTPUT_CHARS:]
        parts.append("output:\n" + out)
    if ctx.get("recent"):
        parts.append("recent terminal lines:\n" + ctx["recent"][-MAX_OUTPUT_CHARS:])
    if ctx.get("screen"):
        parts.append("visible screen:\n" + ctx["screen"][-MAX_OUTPUT_CHARS:])
    parts.append("</terminal_context>")
    return "\n".join(parts)


def context_label(ctx: Dict[str, Any]) -> str:
    bits = []
    if ctx.get("last"):
        bits.append(f"{ctx['last'].get('command')} (exit {ctx['last'].get('exit')})")
    elif ctx.get("recent"):
        bits.append("recent output")
    if ctx.get("screen"):
        bits.append("screen")
    return "+ " + (", ".join(bits) if bits else "cwd")
