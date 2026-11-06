"""Bottom-docked panel: an iTerm2 *browser* split pane showing the panel page,
toggled per tab with Ctrl+Shift+. (Intelligent Terminal's agent-pane shortcut).
It's created once per tab; hiding maximizes the terminal pane (View > Maximize
Active Pane), which keeps the panel loaded so showing it again is instant.

The split is left at iTerm2's default size: resizing it via update_layout mixes
terminal rows with browser-pane points and makes iTerm2 resize the window.

iTerm2 has browser profiles ("Custom Command" = "Browser" + "Initial URL"); we
apply them as per-session customizations on a split, so no profile is added
to the user's iTerm2 settings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Optional
from urllib.parse import quote

import iterm2

from .terminal import ItermTerminal

log = logging.getLogger(__name__)

STATE_VAR = "user.claude_panel"  # per terminal session: "open" / "closed"
MAXIMIZE_PANE = iterm2.MainMenu.View.MAXIMIZE_ACTIVE_PANE.value.identifier

# iTerm2 shows a URL/navigation bar on browser panes. Newer iTerm2 builds have a
# session function for it, which works before the pane is even drawn, so the bar
# never appears. Older ones only have Web > Show Toolbar (acting on the focused
# browser pane), which we press via System Events; that needs Accessibility
# permission for iTerm2, and without it the bar simply stays.
HIDE_TOOLBAR_FUNCTION = "iterm2.browser_set_toolbar_hidden(hidden: true)"
# Started before the split so osascript's own startup overlaps it: waits for a
# browser pane to have focus (the item is disabled for terminals), then presses
# it. (Its checkmark can lag behind the pane by a second, so it isn't consulted.)
HIDE_TOOLBAR_SCRIPT = """
tell application "System Events"
  tell process "iTerm2"
    if frontmost is false then return "background"
    set mi to menu item "Show Toolbar" of menu "Web" of menu bar 1
    repeat 80 times
      if enabled of mi then
        click mi
        return "hidden"
      end if
      delay 0.025
    end repeat
    return "disabled"
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
        self._toolbar_function: Optional[bool] = None  # iTerm2 has HIDE_TOOLBAR_FUNCTION?
        terminal.is_panel = self.is_panel

    def url_for(self, tab_id: str) -> str:
        """Each tab's pane shows that tab's conversation."""
        return self.url + "&tab=" + quote(tab_id, safe="")

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
            # Hide/show by maximizing the terminal pane rather than closing the
            # panel: it stays loaded, so showing it again doesn't flash.
            current = self.app.get_tab_by_id(tab.tab_id) or tab
            hidden = any(s.session_id == panel.session_id for s in current.minimized_sessions)
            await session.async_activate()
            await iterm2.MainMenu.async_select_menu_item(self.connection, MAXIMIZE_PANE)
            if hidden:
                await panel.async_activate()
            await _set_state(session, "open" if hidden else "closed")
            return {"state": "open" if hidden else "closed"}
        self._panels.pop(tab.tab_id, None)  # stale: closed by the user

        window = session.window
        frame = await window.async_get_frame() if window else None
        colors = await _color_properties(session)
        profile = iterm2.LocalWriteOnlyProfile()
        profile._simple_set("Custom Command", "Browser")  # pylint: disable=protected-access
        profile._simple_set("Initial URL", self.url_for(tab.tab_id))  # pylint: disable=protected-access
        keep_toolbar = os.environ.get("INTELLIGENT_ITERM_KEEP_BROWSER_TOOLBAR") == "1"
        menu_click = None
        if not keep_toolbar and not self._toolbar_function:
            menu_click = asyncio.ensure_future(hide_browser_toolbar())
        panel = await session.async_split_pane(vertical=False, profile_customizations=profile)
        self._panels[tab.tab_id] = panel.session_id
        # Both right away and together: the pane's URL bar and its black
        # background both show for as long as these take.
        hide = (_hide_toolbar_by_function(panel)
                if not keep_toolbar and self._toolbar_function is not False else _none())
        hidden, _ = await asyncio.gather(hide, _set_colors(panel, colors))
        if hidden is not None:
            self._toolbar_function = hidden
        if hidden and menu_click is not None:
            # First open only (we didn't know yet). If the click got in first it
            # toggled the bar back on; the function call is idempotent.
            menu_click.cancel()
            await asyncio.gather(menu_click, return_exceptions=True)
            await _hide_toolbar_by_function(panel)
        # The new pane's focus event can arrive before we knew it was a panel.
        self.terminal.remember_terminal(tab.tab_id, session.session_id)
        await _set_state(session, "open")
        await _set_state(panel, "open")  # the panel shows the window's status bar while focused
        if window is not None and frame is not None:
            # iTerm2 snaps the window to whole rows after a split, shrinking it a
            # few points each time; put the original size back.
            await asyncio.sleep(0.15)
            await window.async_set_frame(frame)
        if menu_click is not None and not menu_click.cancelled():
            await menu_click
        return {"state": "open"}

    async def replace_restored_panels(self, windows=None) -> int:
        """iTerm2 restores browser panes on relaunch with their old URL, which points
        at a previous run's server (and shows the URL bar). Swap each for a fresh one."""
        stale = []
        for window in (windows if windows is not None else self.app.terminal_windows):
            for tab in window.tabs:
                for s in tab.all_sessions:
                    try:
                        props = (await s.async_get_profile()).all_properties
                    except Exception:  # noqa: BLE001
                        continue
                    url = props.get("Initial URL") or ""
                    if (props.get("Custom Command") == "Browser" and "dock=1" in url
                            and url.startswith("http://127.0.0.1:") and not url.startswith(self.url)):
                        stale.append((tab, s))
        for tab, panel in stale:
            terminal = next((x for x in tab.all_sessions if x.session_id != panel.session_id), None)
            await panel.async_close(force=True)
            if terminal is not None:
                await asyncio.sleep(0.3)
                await self.toggle(terminal)
        if stale:
            log.info("replaced %d restored panel pane(s)", len(stale))
        return len(stale)

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
        """Forget panels the user closed themselves (pane ✕) and fix the button
        label; close a panel once the last terminal in its tab has exited, so
        `exit` closes the tab as it would without the panel."""
        async with iterm2.SessionTerminationMonitor(self.connection) as mon:
            while True:
                gone = await mon.async_get()
                log.debug("session ended: %s", gone)
                try:
                    await self._session_ended(gone)
                except Exception:  # noqa: BLE001 - keep watching
                    log.exception("handling ended session failed")

    async def _session_ended(self, gone: str) -> None:
        for tab_id, panel_id in list(self._panels.items()):
            tab = self.app.get_tab_by_id(tab_id)
            if panel_id == gone:
                del self._panels[tab_id]
                for s in (tab.sessions if tab else []):
                    await _set_state(s, "closed")
            elif tab is None:
                del self._panels[tab_id]  # whole tab closed
            elif not any(s.session_id != gone and not self.is_panel(s.session_id)
                         for s in tab.sessions):
                del self._panels[tab_id]
                panel = self.app.get_session_by_id(panel_id)
                if panel is not None:
                    try:
                        await panel.async_close(force=True)
                    except Exception:  # noqa: BLE001 - already closing
                        log.info("could not close orphaned panel", exc_info=True)

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


async def _hide_toolbar_by_function(panel: iterm2.Session) -> Optional[bool]:
    """Hide the pane's URL bar with iTerm2's session function. False if this
    iTerm2 doesn't have it; None if it failed for another reason."""
    try:
        await panel.async_invoke_function(HIDE_TOOLBAR_FUNCTION)
        log.info("hide browser toolbar: hidden by function")
        return True
    except Exception as e:  # noqa: BLE001 - fall back to the menu
        log.info("hide browser toolbar: function failed (%s), using the menu", e)
        return False if "No function registered" in str(e) else None


async def hide_browser_toolbar() -> str:
    """Press Web > Show Toolbar once a browser pane has focus, if its bar is
    showing. Returns what happened."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript", "-e", HIDE_TOOLBAR_SCRIPT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 5)
        result = out.decode().strip() or err.decode().strip()
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        raise
    except Exception as e:  # noqa: BLE001 - cosmetic
        result = f"failed: {e}"
    log.info("hide browser toolbar: %s", result)
    return result


async def _none() -> None:
    return None


async def _set_colors(panel: iterm2.Session, colors: iterm2.LocalWriteOnlyProfile) -> None:
    """A browser pane starts black and ignores colors passed with the split;
    with the Minimal theme the window's title and tab bar follow the focused
    pane, so the whole window flashed. Give it the terminal's colors."""
    try:
        await panel.async_set_profile_properties(colors)
    except Exception:  # noqa: BLE001 - cosmetic
        log.info("could not copy terminal colors", exc_info=True)


async def _color_properties(session: iterm2.Session) -> iterm2.LocalWriteOnlyProfile:
    colors = iterm2.LocalWriteOnlyProfile()
    try:
        for key, value in (await session.async_get_profile()).all_properties.items():
            if "Color" in key or key == "Use Separate Colors for Light and Dark Mode":
                colors._simple_set(key, value)  # pylint: disable=protected-access
    except Exception:  # noqa: BLE001 - cosmetic
        log.info("could not read terminal colors", exc_info=True)
    return colors


async def _set_state(session: iterm2.Session, state: str) -> None:
    try:
        await session.async_set_variable(STATE_VAR, state)
    except Exception:  # noqa: BLE001 - only affects the status bar label
        log.info("could not set %s", STATE_VAR, exc_info=True)
