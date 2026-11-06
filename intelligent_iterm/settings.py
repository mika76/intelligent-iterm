"""The panel's own preferences, kept across restarts in a small JSON file:

    {"agent": "claude",                                  default agent for new conversations
     "options": {"claude": {"model": "haiku"}},          header picks, per agent
     "agents": [{"id": "codex", "name": "Codex",         extra ACP agents (optional)
                 "command": ["npx", "@zed-industries/codex-acp"]}]}

Nothing secret is stored here.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(os.path.expanduser("~/Library/Application Support/intelligent-iterm/settings.json"))


class Settings:
    """With no path, kept in memory only (tests, dev server)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.data: Dict[str, Any] = {}
        if path is not None:
            try:
                data = json.loads(path.read_text())
                self.data = data if isinstance(data, dict) else {}
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                log.warning("ignoring unreadable settings file %s", path, exc_info=True)

    @property
    def agent(self) -> Optional[str]:
        value = self.data.get("agent")
        return str(value) if value else None

    def set_agent(self, agent_id: str) -> None:
        self.data["agent"] = agent_id
        self._save()

    def custom_agents(self) -> List[Dict[str, Any]]:
        agents = self.data.get("agents")
        return [a for a in agents if isinstance(a, dict)] if isinstance(agents, list) else []

    def option(self, agent_id: str, key: str) -> Optional[str]:
        value = ((self.data.get("options") or {}).get(agent_id) or {}).get(key)
        return str(value) if value is not None else None

    def set_option(self, agent_id: str, key: str, value: str) -> None:
        self.data.setdefault("options", {}).setdefault(agent_id, {})[key] = value
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1) + "\n")
            tmp.replace(self.path)
        except OSError:
            log.warning("could not save settings to %s", self.path, exc_info=True)
