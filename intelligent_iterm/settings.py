"""The panel's own preferences (model, mode, effort), kept across restarts in a
small JSON file. Nothing secret is stored here."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(os.path.expanduser("~/Library/Application Support/intelligent-iterm/settings.json"))


class Settings:
    """Picker choices: config option id -> value. With no path, kept in memory only."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.values: Dict[str, str] = {}
        if path is not None:
            try:
                data = json.loads(path.read_text())
                self.values = {str(k): str(v) for k, v in (data.get("options") or {}).items()}
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                log.warning("ignoring unreadable settings file %s", path, exc_info=True)

    def get(self, key: str) -> Optional[str]:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"options": self.values}, indent=1) + "\n")
            tmp.replace(self.path)
        except OSError:
            log.warning("could not save settings to %s", self.path, exc_info=True)
