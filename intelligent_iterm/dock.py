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
import contextlib
import io
import logging
import os
from typing import Dict, Optional
from urllib.parse import quote

import iterm2

from .terminal import ItermTerminal
from .web import EventHub

log = logging.getLogger(__name__)

STATE_VAR = "user.claude_panel"  # per terminal session: "open" / "closed"
AGENT_VAR = "user.panel_agent"   # per session: the tab's agent, for the status bar label

# Status bar icon: a window with a pane docked along its bottom. iTerm2 draws
# it as a template image tinted to the status bar's text color.
ICON_1X = 'iVBORw0KGgoAAAANSUhEUgAAABAAAAARCAYAAADUryzEAAAA+ElEQVR42p2UQW7CQAxFn0PWhRZUiSNwhK667C24RtXjdMWeI7FoFwgJgdqEKmG6+ROZUUgClryYsf39x/oe49KMYRbaLjOG2yjtaEI14KGn+AeoXE2D9gZsgAIogVPipWJbYBlZm5By4BtYAyud6xbaFfAKfADPwC4GJwq+DHj/XNQXqFOcai2gXN3qliGfganLbwDiIP/EpOpgUCrXUoAAjIHHHgZP/jJ3wQL4FAvrEFDeBoDQxzcoMXgAcxT7CmPuKFI34FdiyVxClwdgn+p6KWFUYhISPyt2BN69Ev0uzOThyhMMOABffhfu2cbs2v7bwD+hGfY/phFCaurDxZoAAAAASUVORK5CYII='
ICON_2X = 'iVBORw0KGgoAAAANSUhEUgAAACAAAAAiCAYAAAA+stv/AAABpElEQVR42u3XvW7UQBQF4M9eJ0HZCClVykjQ0NKg5BFQWmoeBYmC50iZgtegQgJEB0iAaPICIUFhWS/NtWSZ2T97wiKUK43Wsj137tw55/gst7HhKAY+XzVm604oMcq40TLGSjssUcf1GNsDF/+Fi0TuZAFFtOs+XuAYO3G/6Nn2n3iPZ3ibKqK98wL3cB4Jco7veBRrjVIdGGGKl3iCa5zh66KqlwB4hgM8xR5e4yieJY9iHLuvcZoRhM8j5w8ctjqu6ry4E/cKfIqubAWQ+kQVcz9Gzgq73Rfm8XU7jqQYUEDDgjaT6i7wsolHn1zlpqV44wVUS2hU9BShVI61C5jG2U0GbHDSAuLaBYyx36LSEBreXfa5bVRrH5/j9yrUMEdshRLCg9CFEvWiDux2ReNvg7DObEjKviz4v3WgvCEPuLLPrBK8nWb+DrRxMAuH9EcHGpBc4E1UOcnkhBohKvAB31KGpG3JHuLyBizZDI+7lmweJo7wKhxMHWPdxZp513iHkxTultnyQ9wZePYTfJlnyxd1IjcjRn3+epWZGXAb/178BmR+mT0V0bySAAAAAElFTkSuQmCC'
MAXIMIZE_PANE = iterm2.MainMenu.View.MAXIMIZE_ACTIVE_PANE.value.identifier

# iTerm2 shows a URL/navigation bar on browser panes. iTerm2 3.7.7+ has a session
# method that hides it, which works before the pane is shown. Older builds only
# have Web > Show Toolbar (acting on the focused browser pane), which we press via
# System Events; that needs Accessibility permission, and without it the bar stays.
HIDE_TOOLBAR_METHOD = "iterm2.browser_set_toolbar_hidden(hidden: true)"
HIDE_TOOLBAR_SCRIPT = """
tell application "System Events"
  tell process "iTerm2"
    if frontmost is false then return "background"
    set mi to menu item "Show Toolbar" of menu "Web" of menu bar 1
    repeat 40 times
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
PAGE_TIMEOUT = 3.0  # longest to wait for a new pane's page to connect
# iTerm2's Minimal theme tints the window from the page color WebKit reports when
# the page finishes loading; for a page not yet drawn that's WebKit's light
# default, and a later correction is rate limited (or ignored mid-load, leaving
# the pane light for good). So the docked page is kept loading (web.py /hold)
# until it has been on screen this long.
SETTLE_DELAY = 0.2


class Dock:
    def __init__(self, connection: iterm2.Connection, app: iterm2.App,
                 terminal: ItermTerminal, url: str, hub: Optional[EventHub] = None) -> None:
        self.connection = connection
        self.app = app
        self.terminal = terminal
        self.url = url + "&dock=1"
        self._panels: Dict[str, str] = {}  # tab_id -> panel session_id
        self._agents: Dict[str, str] = {}  # tab_id -> agent name, for the status bar label
        self.default_agent_name = lambda: "Agent"  # set by main: the agent new tabs get
        self.hub = hub  # knows when a docked page connected; holds its load open
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
        if self.hub is not None:
            # Keep the page loading until it has been drawn: see SETTLE_DELAY.
            self.hub.hold(tab.tab_id)
        panel = await session.async_split_pane(vertical=False, profile_customizations=profile)
        self._panels[tab.tab_id] = panel.session_id
        # The new pane's focus event can arrive before we knew it was a panel.
        self.terminal.remember_terminal(tab.tab_id, session.session_id)
        # Right away and together, while the new pane is still blank: its URL
        # bar, and its colors (it starts black and ignores colors passed with
        # the split; the Minimal theme tints the window from them until the
        # page reports its own).
        toolbar = None if keep_toolbar else _hide_toolbar_by_method(panel)
        hidden, _ = await asyncio.gather(toolbar or _none(), _set_colors(panel, colors))
        await _set_state(session, "open")
        await _set_state(panel, "open")  # the panel shows the window's status bar while focused
        await _set_var(panel, AGENT_VAR, self._agents.get(tab.tab_id) or self.default_agent_name())
        if window is not None and frame is not None:
            # iTerm2 snaps the window to whole rows after a split, shrinking it a
            # few points each time; put the original size back.
            await asyncio.sleep(0.15)
            await window.async_set_frame(frame)
        if self.hub is not None:
            if not await self.hub.wait_pinned(tab.tab_id, PAGE_TIMEOUT):
                log.info("panel page didn't connect in %ss", PAGE_TIMEOUT)
            await asyncio.sleep(SETTLE_DELAY)
            self.hub.release(tab.tab_id)
        if toolbar is not None and not hidden:
            await hide_browser_toolbar()  # older iTerm2: once the pane has focus
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

    async def set_agent_label(self, tab_id: str, name: str) -> None:
        """The tab's conversation uses `name`: show it on the status bar button."""
        self._agents[tab_id] = name
        tab = self.app.get_tab_by_id(tab_id)
        for s in (tab.all_sessions if tab else []):
            await _set_var(s, AGENT_VAR, name)

    async def register_status_bar_button(self) -> None:
        """A status bar component (panel icon + "Copilot ▴/▾", the tab's agent) that
        toggles the dock when clicked. Add it once via Settings > Profiles > Session >
        Configure Status Bar; it's listed as "Agent panel"."""
        component = iterm2.StatusBarComponent(
            short_description="Agent panel",
            detailed_description="Show or hide the agent panel below this session (⌃⇧.)",
            knobs=[], exemplar="Claude Code ▴", update_cadence=None,
            identifier="com.intelligent-iterm.toggle",
            icons=_icons())

        @iterm2.StatusBarRPC
        async def claude_panel_button(knobs, state=iterm2.Reference(STATE_VAR + "?"),
                                      agent=iterm2.Reference(AGENT_VAR + "?")):
            name = agent or self.default_agent_name()
            return f"{name} ▾" if state == "open" else f"{name} ▴"

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
        await asyncio.gather(self._watch_terminations(), self._watch_layout())

    async def _watch_terminations(self) -> None:
        async with iterm2.SessionTerminationMonitor(self.connection) as mon:
            while True:
                gone = await mon.async_get()
                log.debug("session ended: %s", gone)
                try:
                    await self._session_ended(gone)
                except Exception:  # noqa: BLE001 - keep watching
                    log.exception("handling ended session failed")

    async def _watch_layout(self) -> None:
        """Backstop for the termination monitor, whose notification for a closed
        pane sometimes comes late or not at all: after any layout change, treat
        a panel whose session is gone as ended."""
        async with iterm2.LayoutChangeMonitor(self.connection) as mon:
            while True:
                await mon.async_get()
                await asyncio.sleep(0.3)  # let the app object take in the new layout
                try:
                    await self.forget_closed_panels()
                except Exception:  # noqa: BLE001 - keep watching
                    log.exception("checking for closed panels failed")

    async def forget_closed_panels(self) -> None:
        # get_session_by_id also finds hidden (minimized) panels.
        missing = [p for p in self._panels.values() if self.app.get_session_by_id(p) is None]
        if not missing:
            return
        await self.app.async_refresh()  # the cached layout may lag behind (e.g. a pane just split)
        for panel_id in missing:
            if panel_id in self._panels.values() and self.app.get_session_by_id(panel_id) is None:
                log.info("panel %s is gone without a termination notice", panel_id)
                await self._session_ended(panel_id)

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


async def _hide_toolbar_by_method(panel: iterm2.Session) -> bool:
    """Hide the pane's URL bar with iTerm2's session method (3.7.7+)."""
    try:
        # A session method: it must be sent to the session (as receiver).
        await iterm2.rpc.async_invoke_method(
            panel.connection, panel.session_id, HIDE_TOOLBAR_METHOD, -1)
        return True
    except Exception as e:  # noqa: BLE001 - fall back to the menu
        log.info("hide browser toolbar: method failed (%s), using the menu", e)
        return False


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


async def _none() -> None:
    return None


async def _set_colors(panel: iterm2.Session, colors: iterm2.LocalWriteOnlyProfile) -> None:
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


def _icons() -> list:
    # iterm2's Icon() prints the raw PNG bytes; keep them out of the log.
    with contextlib.redirect_stdout(io.StringIO()):
        return [iterm2.StatusBarComponent.Icon(1, ICON_1X), iterm2.StatusBarComponent.Icon(2, ICON_2X)]


async def _set_var(session: iterm2.Session, name: str, value: str) -> None:
    try:
        await session.async_set_variable(name, value)
    except Exception:  # noqa: BLE001 - only affects the status bar label
        log.info("could not set %s", name, exc_info=True)


async def _set_state(session: iterm2.Session, state: str) -> None:
    try:
        await session.async_set_variable(STATE_VAR, state)
    except Exception:  # noqa: BLE001 - only affects the status bar label
        log.info("could not set %s", STATE_VAR, exc_info=True)
