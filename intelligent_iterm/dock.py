"""Bottom-docked panel: an iTerm2 *browser* split pane showing the panel page,
toggled per tab with Ctrl+Shift+. (Intelligent Terminal's agent-pane shortcut).

iTerm2 has browser profiles ("Custom Command" = "Browser" + "Initial URL"); we
apply them as per-session customizations on a split, so no profile is added
to the user's iTerm2 settings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Optional

import iterm2

from .terminal import ItermTerminal

log = logging.getLogger(__name__)

DOCK_FRACTION = 0.35   # of the split pane's height
MIN_ROWS = 10
STATE_VAR = "user.claude_panel"  # per terminal session: "open" / "closed"

# iTerm2 shows a URL/navigation bar on browser panes. There's no profile key or
# API for it, only the Web > Show Toolbar menu item (which acts on the focused
# browser pane), so we press it via System Events. Needs Accessibility
# permission for iTerm2; if that's missing the bar simply stays.
HIDE_TOOLBAR_SCRIPT = """
tell application "System Events"
  tell process "iTerm2"
    if frontmost is false then return "background"
    set mi to menu item "Show Toolbar" of menu "Web" of menu bar 1
    if enabled of mi is false then return "disabled"
    click mi
    return "hidden"
  end tell
end tell
"""


class Dock:
    def __init__(self, connection: iterm2.Connection, app: iterm2.App,
                 terminal: ItermTerminal, url: str) -> None:
        self.connection = connection
        self.app = app
        self.terminal = terminal
        self.url = url + "&dock=1"
        self._panels: Dict[str, str] = {}  # tab_id -> panel session_id
        terminal.is_panel = self.is_panel

    def is_panel(self, session_id: str) -> bool:
        return session_id in self._panels.values()

    async def toggle(self, session: Optional[iterm2.Session] = None) -> Dict[str, str]:
        """Open or close the dock in `session`'s tab (default: the active terminal)."""
        session = session or self.terminal.active_session()
        if session is None or session.tab is None:
            raise RuntimeError("no active terminal session")
        tab = session.tab
        existing = self._panels.get(tab.tab_id)
        panel = self.app.get_session_by_id(existing) if existing else None
        if panel is not None:
            await panel.async_close(force=True)
            del self._panels[tab.tab_id]
            await _set_state(session, "closed")
            await session.async_activate()
            return {"state": "closed"}
        self._panels.pop(tab.tab_id, None)  # stale: closed by the user

        # Live size, not the cached session.grid_size, which can lag after the
        # panel was closed; a stale size would make update_layout shrink the window.
        try:
            height = int(await session.async_get_variable("rows"))
            width = int(await session.async_get_variable("columns"))
        except (TypeError, ValueError):
            height = width = 0
        profile = iterm2.LocalWriteOnlyProfile()
        profile._simple_set("Custom Command", "Browser")  # pylint: disable=protected-access
        profile._simple_set("Initial URL", self.url)      # pylint: disable=protected-access
        panel = await session.async_split_pane(vertical=False, profile_customizations=profile)
        self._panels[tab.tab_id] = panel.session_id
        # The new pane's focus event can arrive before we knew it was a panel.
        self.terminal.remember_terminal(tab.tab_id, session.session_id)
        await _set_state(session, "open")
        await _set_state(panel, "open")  # the panel shows the window's status bar while focused

        if height > 2 * MIN_ROWS:
            rows = max(MIN_ROWS, int(height * DOCK_FRACTION))
            session.preferred_size = iterm2.Size(width, height - rows)
            panel.preferred_size = iterm2.Size(width, rows)
            try:
                await tab.async_update_layout()
            except Exception:  # noqa: BLE001 - cosmetic; the split still works
                log.info("could not resize dock", exc_info=True)
        if os.environ.get("INTELLIGENT_ITERM_KEEP_BROWSER_TOOLBAR") != "1":
            await asyncio.sleep(0.4)  # let the new pane take focus
            await hide_browser_toolbar()
        return {"state": "open"}

    async def register_status_bar_button(self) -> None:
        """A status bar component ("Claude ▴/▾") that toggles the dock when clicked.
        Add it once via Settings > Profiles > Session > Configure Status Bar."""
        component = iterm2.StatusBarComponent(
            short_description="Claude panel",
            detailed_description="Show or hide the Claude panel below this session (⌃⇧.)",
            knobs=[], exemplar="✳ Claude ▴", update_cadence=None,
            identifier="com.intelligent-iterm.toggle")

        @iterm2.StatusBarRPC
        async def claude_panel_button(knobs, state=iterm2.Reference(STATE_VAR + "?")):
            return "✳ Claude ▾" if state == "open" else "✳ Claude ▴"

        async def onclick(session_id: str) -> None:
            session = self.app.get_session_by_id(session_id)
            if session is not None and self.is_panel(session_id):
                # Clicked while the panel is focused: close it, back to its terminal.
                session = next((s for s in session.tab.sessions if not self.is_panel(s.session_id)), None)
            if session is not None:
                await self.toggle(session)

        await component.async_register(self.connection, claude_panel_button, onclick=onclick)

    async def watch_closed_panels(self) -> None:
        """Forget panels the user closed themselves (pane ✕), and fix the button label."""
        async with iterm2.SessionTerminationMonitor(self.connection) as mon:
            while True:
                gone = await mon.async_get()
                log.debug("session ended: %s", gone)
                for tab_id, panel_id in list(self._panels.items()):
                    if panel_id == gone:
                        del self._panels[tab_id]
                        tab = self.app.get_tab_by_id(tab_id)
                        for s in (tab.sessions if tab else []):
                            await _set_state(s, "closed")

    async def watch_hotkey(self) -> None:
        """Ctrl+Shift+. in any terminal session toggles the dock for that tab."""
        pattern = iterm2.KeystrokePattern()
        pattern.required_modifiers = [iterm2.Modifier.CONTROL, iterm2.Modifier.SHIFT]
        pattern.forbidden_modifiers = [iterm2.Modifier.COMMAND, iterm2.Modifier.OPTION]
        pattern.keycodes = [iterm2.Keycode.ANSI_PERIOD]
        async with iterm2.KeystrokeMonitor(self.connection) as mon, \
                iterm2.KeystrokeFilter(self.connection, [pattern]):
            while True:
                stroke = await mon.async_get()
                if (stroke.keycode == iterm2.Keycode.ANSI_PERIOD
                        and iterm2.Modifier.CONTROL in stroke.modifiers
                        and iterm2.Modifier.SHIFT in stroke.modifiers
                        and iterm2.Modifier.COMMAND not in stroke.modifiers
                        and iterm2.Modifier.OPTION not in stroke.modifiers):
                    try:
                        await self.toggle()
                    except Exception:  # noqa: BLE001
                        log.exception("dock toggle failed")


async def hide_browser_toolbar() -> str:
    """Press Web > Show Toolbar for the focused browser pane. Returns what happened."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript", "-e", HIDE_TOOLBAR_SCRIPT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 5)
        result = out.decode().strip() or err.decode().strip()
    except Exception as e:  # noqa: BLE001 - cosmetic
        result = f"failed: {e}"
    log.info("hide browser toolbar: %s", result)
    return result


async def _set_state(session: iterm2.Session, state: str) -> None:
    try:
        await session.async_set_variable(STATE_VAR, state)
    except Exception:  # noqa: BLE001 - only affects the status bar label
        log.info("could not set %s", STATE_VAR, exc_info=True)
