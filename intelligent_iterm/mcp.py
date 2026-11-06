"""Minimal MCP server (Streamable HTTP, JSON responses only) that gives the agent
tools for the terminal, in the style of Intelligent Terminal's action handoff:
the agent *proposes* a command and the user accepts it in the panel.

Served by WebServer at /mcp behind the same per-launch token as the UI.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

SERVER_NAME = "iterm"  # Claude Code names the tools mcp__iterm__<tool>
DEFAULT_PROTOCOL = "2025-06-18"

ToolFn = Callable[[Dict[str, Any]], Awaitable[str]]


class Tool:
    def __init__(self, name: str, description: str, schema: Dict[str, Any], fn: ToolFn) -> None:
        self.name, self.description, self.schema, self.fn = name, description, schema, fn

    def spec(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


class ToolError(Exception):
    """Raised by a tool for a result the agent should see as an error (and may correct)."""


class McpServer:
    def __init__(self, tools: List[Tool]) -> None:
        self.tools = {t.name: t for t in tools}

    def allowed_tool_names(self) -> List[str]:
        return [f"mcp__{SERVER_NAME}__{name}" for name in self.tools]

    async def handle_http(self, body: Any) -> Tuple[int, Optional[Dict[str, Any]]]:
        """One POSTed JSON-RPC message -> (HTTP status, response or None)."""
        if not isinstance(body, dict) or "method" not in body:
            return 202, None  # a response/notification from the client: nothing to send
        if "id" not in body:
            return 202, None  # notification (e.g. notifications/initialized)
        try:
            result = await self._call(body["method"], body.get("params") or {})
            return 200, {"jsonrpc": "2.0", "id": body["id"], "result": result}
        except KeyError as e:
            return 200, _error(body["id"], -32601, f"unknown method or tool: {e}")

    async def _call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "intelligent-iterm", "version": "0.1"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.spec() for t in self.tools.values()]}
        if method == "tools/call":
            tool = self.tools[params.get("name")]
            try:
                text = await tool.fn(params.get("arguments") or {})
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except ToolError as e:
                return {"content": [{"type": "text", "text": str(e)}], "isError": True}
            except Exception as e:  # noqa: BLE001 - report to the agent, keep serving
                log.exception("tool %s failed", tool.name)
                return {"content": [{"type": "text", "text": f"tool failed: {e}"}], "isError": True}
        raise KeyError(method)


def _error(rid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
