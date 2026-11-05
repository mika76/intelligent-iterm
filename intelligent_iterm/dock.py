"""Bottom-docked panel: an iTerm2 *browser* split pane showing the panel page,
toggled per tab with Ctrl+Shift+. (Intelligent Terminal's agent-pane shortcut).

iTerm2 has browser profiles ("Custom Command" = "Browser" + "Initial URL"); we
apply them as per-session customizations on a split, so no profile is added
to the user's iTerm2 settings.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import iterm2

from .terminal import ItermTerminal

log = logging.getLogger(__name__)

DOCK_FRACTION = 0.35   # of the split pane's height
MIN_ROWS = 10


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

    async def toggle(self) -> Dict[str, str]:
        session = self.terminal.active_session()
        if session is None or session.tab is None:
            raise RuntimeError("no active terminal session")
        tab = session.tab
        existing = self._panels.get(tab.tab_id)
        panel = self.app.get_session_by_id(existing) if existing else None
        if panel is not None:
            await panel.async_close(force=True)
            del self._panels[tab.tab_id]
            await session.async_activate()
            return {"state": "closed"}
        self._panels.pop(tab.tab_id, None)  # stale: closed by the user

        grid = session.grid_size
        profile = iterm2.LocalWriteOnlyProfile()
        profile._simple_set("Custom Command", "Browser")  # pylint: disable=protected-access
        profile._simple_set("Initial URL", self.url)      # pylint: disable=protected-access
        panel = await session.async_split_pane(vertical=False, profile_customizations=profile)
        self._panels[tab.tab_id] = panel.session_id
        # The new pane's focus event can arrive before we knew it was a panel.
        self.terminal.remember_terminal(tab.tab_id, session.session_id)

        if grid is not None and grid.height > 2 * MIN_ROWS:
            rows = max(MIN_ROWS, int(grid.height * DOCK_FRACTION))
            session.preferred_size = iterm2.Size(grid.width, grid.height - rows)
            panel.preferred_size = iterm2.Size(grid.width, rows)
            try:
                await tab.async_update_layout()
            except Exception:  # noqa: BLE001 - cosmetic; the split still works
                log.info("could not resize dock", exc_info=True)
        return {"state": "open"}

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
