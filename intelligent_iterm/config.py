"""Locate node, the ACP adapter and the claude CLI.

iTerm2 AutoLaunch scripts don't get your shell's PATH (no .zshrc, no nvm), so
we look in the usual places. Override with INTELLIGENT_ITERM_NODE /
INTELLIGENT_ITERM_CLAUDE if needed.
"""
from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .acp import agent_env

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "node_modules" / "@agentclientprotocol" / "claude-agent-acp" / "dist" / "index.js"
HOME = os.path.expanduser("~")


def _version_key(path: str) -> Tuple[int, ...]:
    ver = path.split("/node/v")[-1].split("/")[0]
    return tuple(int(x) for x in ver.split(".") if x.isdigit())


def find_node() -> Optional[str]:
    if os.environ.get("INTELLIGENT_ITERM_NODE"):
        return os.environ["INTELLIGENT_ITERM_NODE"]
    nvm = sorted(glob.glob(f"{HOME}/.nvm/versions/node/v*/bin/node"), key=_version_key, reverse=True)
    candidates = [shutil.which("node"), *nvm, "/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for c in candidates:
        if c and os.access(c, os.X_OK):
            return c
    return None


def find_claude() -> Optional[str]:
    if os.environ.get("INTELLIGENT_ITERM_CLAUDE"):
        return os.environ["INTELLIGENT_ITERM_CLAUDE"]
    for c in (shutil.which("claude"), f"{HOME}/.local/bin/claude",
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if c and os.access(c, os.X_OK):
            return c
    return None


def agent_command_and_env() -> Tuple[List[str], Dict[str, str]]:
    node = find_node()
    if not node:
        raise RuntimeError("node (>= 22) not found; set INTELLIGENT_ITERM_NODE")
    if not ADAPTER.exists():
        raise RuntimeError(f"ACP adapter missing at {ADAPTER}; run ./install.sh")
    return [node, str(ADAPTER)], agent_env(find_claude(), os.path.dirname(node))
