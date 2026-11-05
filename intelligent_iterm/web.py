"""Tiny stdlib HTTP server: serves the chat page, a Server-Sent Events stream,
and JSON POST endpoints. Bound to 127.0.0.1 only.

Every request except the page itself must carry the per-launch token (header
`X-Token`, or `?t=` for the SSE stream) so other local web pages can't drive
the panel (e.g. type into your terminal) via cross-site requests.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

Handler = Callable[[Dict[str, Any]], Awaitable[Any]]
STATIC = Path(__file__).parent / "static"
HISTORY_LIMIT = 1000


class EventHub:
    """Fan-out of panel events to SSE clients, with replay so the webview can reload."""

    def __init__(self) -> None:
        self._clients: List[asyncio.Queue] = []
        self._history: List[Dict[str, Any]] = []

    def publish(self, event: Dict[str, Any]) -> None:
        last = self._history[-1] if self._history else None
        if event["type"] == "chunk" and last and last["type"] == "chunk":
            # Store streamed text as one event so replay stays short.
            self._history[-1] = {"type": "chunk", "text": last["text"] + event["text"]}
        else:
            self._history.append(event)
        del self._history[:-HISTORY_LIMIT]
        for q in self._clients:
            q.put_nowait(event)

    def clear_history(self) -> None:
        self._history.clear()

    def subscribe(self) -> Tuple[asyncio.Queue, List[Dict[str, Any]]]:
        q: asyncio.Queue = asyncio.Queue()
        self._clients.append(q)
        return q, list(self._history)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._clients:
            self._clients.remove(q)


class WebServer:
    def __init__(self, token: str, hub: EventHub, routes: Dict[str, Handler]) -> None:
        self.token = token
        self.hub = hub
        self.routes = routes  # path -> async handler(json body) -> json-able result
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
                body = (STATIC / "index.html").read_bytes()
                return await self._send(writer, 200, body, "text/html; charset=utf-8")

            supplied = headers.get("x-token") or (query.get("t") or [""])[0]
            if not hmac.compare_digest(supplied, self.token):
                return await self._send(writer, 403, b"bad token", "text/plain")

            if method == "GET" and url.path == "/events":
                return await self._sse(writer)

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

    async def _sse(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Cache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
        q, backlog = self.hub.subscribe()
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
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 500: "Internal Server Error"}[status]
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
                     f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
        await writer.drain()


def _sse_frame(event: Dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(event).encode() + b"\n\n"
