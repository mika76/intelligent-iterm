"""Minimal Agent Client Protocol (ACP) client.

Spawns an ACP agent (claude-agent-acp) as a subprocess and talks
newline-delimited JSON-RPC 2.0 to it over stdin/stdout.

Only what the side panel needs is implemented: initialize, session/new,
session/set_mode, session/prompt, session/cancel, and answering
session/request_permission. We advertise no fs/terminal capabilities, so
the agent uses its own built-in tools (which still ask permission).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# Called for every session/update notification: (update_dict) -> None
UpdateHandler = Callable[[Dict[str, Any]], Awaitable[None]]
# Called for session/request_permission: (params) -> outcome dict
PermissionHandler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class AcpError(Exception):
    pass


class AcpClient:
    def __init__(
        self,
        command: List[str],
        env: Dict[str, str],
        on_update: UpdateHandler,
        on_permission: PermissionHandler,
    ) -> None:
        self._command = command
        self._env = env
        self._on_update = on_update
        self._on_permission = on_permission
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._ids = itertools.count(1)
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.session_id: Optional[str] = None

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> Dict[str, Any]:
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            limit=32 * 1024 * 1024,  # tool results can be large single lines
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        return await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "intelligent-iterm", "version": "0.1.0"},
            },
        )

    async def new_session(self, cwd: str, system_prompt: str, mode: str = "default",
                          mcp_servers: Optional[List[Dict[str, Any]]] = None,
                          allowed_tools: Optional[List[str]] = None) -> str:
        params: Dict[str, Any] = {
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
            "systemPrompt": {
                "mode": "append",
                "content": [{"type": "text", "text": system_prompt}],
            },
        }
        if allowed_tools:
            # claude-agent-acp passes these to the Claude Agent SDK: our own panel
            # tools run without an extra Allow prompt (they ask the user themselves).
            params["_meta"] = {"claudeCode": {"options": {"allowedTools": allowed_tools}}}
        result = await self.request("session/new", params)
        self.session_id = result["sessionId"]
        modes = (result.get("modes") or {}).get("availableModes") or []
        if any(m.get("id") == mode for m in modes):
            await self.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode})
        return self.session_id

    async def prompt(self, blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.session_id:
            raise AcpError("no session")
        return await self.request("session/prompt", {"sessionId": self.session_id, "prompt": blocks})

    async def cancel(self) -> None:
        if self.session_id:
            await self._send({"jsonrpc": "2.0", "method": "session/cancel",
                              "params": {"sessionId": self.session_id}})

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()
        for t in (self._reader_task, self._stderr_task):
            if t:
                t.cancel()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ---- JSON-RPC plumbing ----------------------------------------------

    async def request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        msg_id = next(self._ids)
        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        return await fut

    async def _send(self, obj: Dict[str, Any]) -> None:
        if not self.alive:
            raise AcpError("agent process is not running")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("non-JSON line from agent: %r", line[:200])
                    continue
                await self._dispatch(msg)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AcpError("agent process exited"))
            self._pending.clear()

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        if "id" in msg and "method" not in msg:  # response to our request
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(AcpError(msg["error"].get("message", str(msg["error"]))))
                else:
                    fut.set_result(msg.get("result") or {})
            return
        method = msg.get("method")
        if method == "session/update":
            await self._on_update(msg["params"]["update"])
        elif method == "session/request_permission" and "id" in msg:
            # Answer asynchronously so the read loop keeps flowing.
            asyncio.ensure_future(self._answer_permission(msg))
        elif "id" in msg:  # any other agent->client request we don't support
            await self._send({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32601, "message": f"Method not found: {method}"}})

    async def _answer_permission(self, msg: Dict[str, Any]) -> None:
        try:
            outcome = await self._on_permission(msg["params"])
        except Exception:  # noqa: BLE001 - never leave the agent hanging
            log.exception("permission handler failed")
            outcome = {"outcome": "cancelled"}
        await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": outcome}})

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            log.info("agent: %s", line.decode(errors="replace").rstrip())


def agent_env(claude_path: Optional[str], node_dir: Optional[str]) -> Dict[str, str]:
    """Environment for the adapter: rely on the Claude Code login, not an API key."""
    env = dict(os.environ)
    if os.environ.get("INTELLIGENT_ITERM_KEEP_API_KEY") != "1":
        env.pop("ANTHROPIC_API_KEY", None)
    if claude_path:
        env["CLAUDE_CODE_EXECUTABLE"] = claude_path
    if node_dir:
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    return env
