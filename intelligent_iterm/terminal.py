"""Everything that touches iTerm2 sessions: reading context, watching for
failed commands, and typing suggested commands (never pressing Enter)."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

import iterm2

log = logging.getLogger(__name__)

MAX_OUTPUT_LINES = 200   # per command
RECENT_LINES = 60        # fallback when shell integration isn't available
KEEP_FAILURES = 50
# Anything that could submit or alter the command line: newlines, ESC, ^C, etc.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


class ItermTerminal:
    def __init__(self, connection: iterm2.Connection, app: iterm2.App,
                 on_failure: Callable[[Dict[str, Any]], None]) -> None:
        self.connection = connection
        self.app = app
        self.on_failure = on_failure
        self._last: Dict[str, Dict[str, Any]] = {}            # session_id -> last command
        self._failures: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    # ---- active session --------------------------------------------------

    def active_session(self) -> Optional[iterm2.Session]:
        window = self.app.current_terminal_window
        tab = window.current_tab if window else None
        return tab.current_session if tab else None

    # ---- watching for finished commands -------------------------------------

    async def watch(self) -> None:
        await iterm2.EachSessionOnceMonitor.async_foreach_session_create_task(
            self.app, self._watch_session)

    async def _watch_session(self, session_id: str) -> None:
        modes = [iterm2.PromptMonitor.Mode.COMMAND_END]
        try:
            async with iterm2.PromptMonitor(self.connection, session_id, modes) as mon:
                while True:
                    _mode, status, prompt_id = await mon.async_get(include_id=True)
                    try:
                        await self._command_ended(session_id, prompt_id, status)
                    except Exception:  # noqa: BLE001 - keep watching
                        log.exception("reading finished command failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - e.g. session closed
            log.info("stopped watching %s", session_id, exc_info=True)

    async def _command_ended(self, session_id: str, prompt_id: Optional[str], status: int) -> None:
        if not prompt_id:
            return
        prompt = await iterm2.async_get_prompt_by_id(self.connection, session_id, prompt_id)
        if prompt is None or not prompt.command:
            return
        record = await self._record(session_id, prompt, status)
        self._last[session_id] = record
        if status != 0:
            self._failures[record["id"]] = record
            while len(self._failures) > KEEP_FAILURES:
                self._failures.popitem(last=False)
            if self._is_active(session_id):
                self.on_failure(record)

    def _is_active(self, session_id: str) -> bool:
        s = self.active_session()
        return s is not None and s.session_id == session_id

    async def _record(self, session_id: str, prompt: iterm2.Prompt, status: Optional[int]) -> Dict[str, Any]:
        session = self.app.get_session_by_id(session_id)
        output = await self._read_range(session, prompt.output_range) if session else ""
        return {"id": f"{session_id}:{prompt.unique_id}", "session_id": session_id,
                "command": prompt.command.strip(), "exit": status,
                "cwd": prompt.working_directory, "output": output}

    async def _read_range(self, session: iterm2.Session, rng: iterm2.CoordRange) -> str:
        first, last = rng.start.y, rng.end.y
        if rng.end.x == 0:
            last -= 1  # range end is exclusive at column 0
        if last < first:
            return ""
        first = max(first, last - MAX_OUTPUT_LINES + 1)
        async with iterm2.Transaction(self.connection):
            info = await session.async_get_line_info()
            first = max(first, info.overflow)  # older lines were dropped from scrollback
            lines = await session.async_get_contents(first, last - first + 1)
        return _join_lines(lines)

    # ---- context for the agent -----------------------------------------------

    def get_failure(self, failure_id: str) -> Optional[Dict[str, Any]]:
        return self._failures.get(failure_id)

    async def get_context(self, include_screen: bool) -> Dict[str, Any]:
        session = self.active_session()
        if session is None:
            return {}
        ctx: Dict[str, Any] = {"cwd": await session.async_get_variable("path")}
        last = self._last.get(session.session_id) or await self._last_finished_prompt(session)
        if last:
            ctx["last"] = last
        else:
            ctx["recent"] = await self._recent_lines(session)
        if include_screen:
            contents = await session.async_get_screen_contents()
            ctx["screen"] = _join_lines([contents.line(i) for i in range(contents.number_of_lines)])
        return ctx

    async def _last_finished_prompt(self, session: iterm2.Session) -> Optional[Dict[str, Any]]:
        """For commands that ran before this script started (needs shell integration)."""
        try:
            ids = await iterm2.async_list_prompts(self.connection, session.session_id)
            for pid in list(ids)[::-1][:3]:
                p = await iterm2.async_get_prompt_by_id(self.connection, session.session_id, pid)
                if p and p.command and p.state == iterm2.PromptState.FINISHED:
                    # Prompt doesn't expose exit status; read it from the proto.
                    proto = getattr(p, "_Prompt__proto", None)
                    status = proto.exit_status if proto is not None and proto.HasField("exit_status") else None
                    return await self._record(session.session_id, p, status)
        except Exception:  # noqa: BLE001 - no shell integration, old iTerm2, …
            log.info("no prompt history", exc_info=True)
        return None

    async def _recent_lines(self, session: iterm2.Session) -> str:
        async with iterm2.Transaction(self.connection):
            info = await session.async_get_line_info()
            end = info.overflow + info.scrollback_buffer_height + info.mutable_area_height
            start = max(info.overflow, end - RECENT_LINES)
            lines = await session.async_get_contents(start, end - start)
        return _join_lines(lines)

    # ---- typing into the terminal ---------------------------------------------

    async def insert(self, text: str) -> None:
        """Type `text` at the prompt of the active session. Never sends Enter."""
        command = text.replace("\t", " ").rstrip()
        if not command.strip():
            raise ValueError("nothing to insert")
        if _CONTROL.search(command):
            raise ValueError("refusing to insert text with newlines or control characters")
        session = self.active_session()
        if session is None:
            raise RuntimeError("no active terminal session")
        await session.async_send_text(command, suppress_broadcast=True)


def _join_lines(lines: List[Any]) -> str:
    """Join screen lines, un-wrapping soft-wrapped ones."""
    out: List[str] = []
    for line in lines:
        out.append(line.string)
        out.append("\n" if line.hard_eol else "")
    return "".join(out).rstrip()
