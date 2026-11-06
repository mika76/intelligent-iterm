"""Tiny stdlib HTTP server: serves the chat page, a Server-Sent Events stream,
and JSON POST endpoints. Bound to 127.0.0.1 only.

Every request except the page itself must carry the per-launch token (header
`X-Token`, or `?t=` for the SSE stream) so other local web pages can't drive
the panel (e.g. type into your terminal) via cross-site requests.
"""
from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

Handler = Callable[[Dict[str, Any]], Awaitable[Any]]
STATIC = Path(__file__).parent / "static"
HISTORY_LIMIT = 1000


MAIN = "main"  # the conversation key when the terminal has no tabs (tests, dev server)


class EventHub:
    """Fan-out of panel events to SSE clients, with replay so the webview can reload.

    Chat history is kept per conversation (one per iTerm2 tab). A client either
    shows one conversation for good (the docked pane of a tab) or follows the
    active tab (the toolbelt): when the active tab changes, followers get a
    "switch" event and the other conversation's history."""

    def __init__(self) -> None:
        self._clients: Dict[asyncio.Queue, Tuple[str, bool]] = {}  # queue -> (conv, pinned)
        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._sticky: Dict[str, Dict[str, Any]] = {}  # latest event per key, e.g. theme
        self.active = MAIN  # what followers show
        self.resolve_active: Optional[Callable[[], str]] = None  # set by Panel: the active tab now

    def get_sticky(self, key: str) -> Optional[Dict[str, Any]]:
        return self._sticky.get(key)

    def set_sticky(self, key: str, event: Dict[str, Any]) -> None:
        """Latest-value state shared by every conversation (theme, agent info):
        replayed first, survives New chat."""
        self._sticky[key] = event
        for q in self._clients:
            q.put_nowait(event)

    def publish(self, event: Dict[str, Any], conv: str = MAIN) -> None:
        history = self._history.setdefault(conv, [])
        last = history[-1] if history else None
        if event["type"] == "chunk" and last and last["type"] == "chunk":
            # Store streamed text as one event so replay stays short.
            history[-1] = {"type": "chunk", "text": last["text"] + event["text"]}
        else:
            history.append(event)
        del history[:-HISTORY_LIMIT]
        for q, (shown, _pinned) in self._clients.items():
            if shown == conv:
                q.put_nowait(event)

    def clear_history(self, conv: str = MAIN) -> None:
        self._history.pop(conv, None)

    def conversations(self) -> List[str]:
        return list(self._history)

    def _backlog(self, conv: str) -> List[Dict[str, Any]]:
        return ([{"type": "switch", "conv": conv}] + list(self._sticky.values())
                + list(self._history.get(conv, [])))

    def subscribe(self, conv: Optional[str] = None) -> Tuple[asyncio.Queue, List[Dict[str, Any]]]:
        """conv: show that conversation for good; None: follow the active tab."""
        q: asyncio.Queue = asyncio.Queue()
        if conv is None and self.resolve_active is not None:
            self.active = self.resolve_active()
        shown = conv or self.active
        self._clients[q] = (shown, conv is not None)
        return q, self._backlog(shown)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.pop(q, None)

    def follow(self, conv: str) -> None:
        """The active tab changed: move followers to its conversation."""
        self.active = conv
        for q, (shown, pinned) in list(self._clients.items()):
            if not pinned and shown != conv:
                self._clients[q] = (conv, False)
                for ev in self._backlog(conv):
                    q.put_nowait(ev)


class WebServer:
    def __init__(self, token: str, hub: EventHub, routes: Dict[str, Handler], mcp: Any = None) -> None:
        self.token = token
        self.hub = hub
        self.routes = routes  # path -> async handler(json body) -> json-able result
        self.mcp = mcp        # McpServer for the agent's tools, served at /mcp
        self.port: Optional[int] = None
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, _, v in
                       (line.partition(":") for line in lines[1:] if line)}
            url = urlsplit(target)
            query = parse_qs(url.query)

            # Block DNS-rebinding: only accept our own host name.
            if headers.get("host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
                return await self._send(writer, 403, b"bad host", "text/plain")

            if method == "GET" and url.path == "/":
                body = render_page(self.hub.get_sticky("theme"))
                return await self._send(writer, 200, body, "text/html; charset=utf-8")

            supplied = headers.get("x-token") or (query.get("t") or [""])[0]
            if not hmac.compare_digest(supplied, self.token):
                return await self._send(writer, 403, b"bad token", "text/plain")

            if method == "GET" and url.path == "/events":
                return await self._sse(writer, (query.get("tab") or [""])[0] or None)

            if url.path == "/mcp" and self.mcp is not None:
                return await self._mcp(method, headers, reader, writer)

            handler = self.routes.get(url.path)
            if method != "POST" or handler is None:
                return await self._send(writer, 404, b"not found", "text/plain")
            length = int(headers.get("content-length") or 0)
            raw = await reader.readexactly(length) if length else b"{}"
            try:
                result = await handler(json.loads(raw or b"{}"))
                await self._send_json(writer, 200, {"ok": True, "result": result})
            except Exception as e:  # noqa: BLE001 - report to the UI
                log.exception("handler %s failed", url.path)
                await self._send_json(writer, 500, {"ok": False, "error": str(e)})
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _mcp(self, method: str, headers: Dict[str, str], reader: asyncio.StreamReader,
                   writer: asyncio.StreamWriter) -> None:
        if method != "POST":  # no server-initiated stream / session deletion
            return await self._send(writer, 405, b"", "text/plain")
        length = int(headers.get("content-length") or 0)
        raw = await reader.readexactly(length) if length else b""
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            return await self._send_json(writer, 400, {"jsonrpc": "2.0", "id": None,
                                                       "error": {"code": -32700, "message": "parse error"}})
        status, response = await self.mcp.handle_http(body, headers.get("x-conversation") or MAIN)
        if response is None:
            return await self._send(writer, status, b"", "text/plain")
        await self._send_json(writer, status, response)

    async def _sse(self, writer: asyncio.StreamWriter, conv: Optional[str]) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Cache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
        q, backlog = self.hub.subscribe(conv)
        try:
            for ev in backlog:
                writer.write(_sse_frame(ev))
            await writer.drain()
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    writer.write(_sse_frame(ev))
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")  # keep-alive
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.hub.unsubscribe(q)

    async def _send_json(self, writer: asyncio.StreamWriter, status: int, obj: Any) -> None:
        await self._send(writer, status, json.dumps(obj).encode(), "application/json")

    async def _send(self, writer: asyncio.StreamWriter, status: int, body: bytes, ctype: str) -> None:
        reason = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  405: "Method Not Allowed", 500: "Internal Server Error"}[status]
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
                     f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
        await writer.drain()


def theme_vars(theme: Dict[str, Any]) -> Dict[str, str]:
    """iTerm2 profile colors/font -> CSS custom properties for the page."""
    out = {f"--{k}": theme[k] for k in
           ("bg", "fg", "dim", "accent", "red", "green", "yellow", "cursor") if theme.get(k)}
    if theme.get("font"):
        out["--font"] = f"'{theme['font']}', ui-monospace, Menlo, monospace"
    if theme.get("size"):
        out["--size"] = f"{max(11, round(theme['size'] * 0.82))}px"
    bg = theme.get("bg") or ""
    if len(bg) == 7:
        r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
        out["color-scheme"] = "dark" if 0.299 * r + 0.587 * g + 0.114 * b < 128 else "light"
    return out


def render_page(theme_event: Optional[Dict[str, Any]]) -> bytes:
    """The chat page, with the current theme baked into <html> so the very first
    paint already matches the terminal (no white flash before SSE delivers it)."""
    page = (STATIC / "index.html").read_text()
    if theme_event and theme_event.get("vars"):
        style = ";".join(f"{k}:{v}" for k, v in theme_event["vars"].items())
        page = page.replace('<html lang="en">',
                            f'<html lang="en" data-theme="iterm" style="{html.escape(style)}">', 1)
    return page.encode()


def _sse_frame(event: Dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(event).encode() + b"\n\n"
