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


# ---- agents ------------------------------------------------------------------
# Claude Code (through the pinned adapter) is the default. Other agents that
# speak ACP are offered when their CLI is installed; more can be added in the
# settings file:  {"agents": [{"id": "codex", "name": "Codex",
#                              "command": ["npx", "@zed-industries/codex-acp"]}]}

class AgentSpec:
    def __init__(self, id: str, name: str, command: List[str], env: Dict[str, str],
                 claude: bool = False) -> None:
        self.id, self.name, self.command, self.env, self.claude = id, name, command, env, claude

    def __repr__(self) -> str:
        return f"AgentSpec({self.id!r}, {self.command!r})"


# (id, name, executable, ACP arguments), checked against each CLI's --help.
KNOWN_AGENTS = [
    ("gemini", "Gemini CLI", "gemini", ["--acp"]),
    ("opencode", "OpenCode", "opencode", ["acp"]),
    ("copilot", "GitHub Copilot", "copilot", ["--acp"]),
]


def _search_path(node: Optional[str]) -> str:
    dirs = [os.path.dirname(node)] if node else []
    dirs += [f"{HOME}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", f"{HOME}/.opencode/bin"]
    return os.pathsep.join(dirs + [os.environ.get("PATH", "")])


def available_agents(custom: Optional[List[Dict]] = None) -> List[AgentSpec]:
    """Claude Code first, then installed known agents, then the user's own."""
    node = find_node()
    path = _search_path(node)
    env = dict(os.environ, PATH=path)
    agents: List[AgentSpec] = []
    try:
        command, claude_env = agent_command_and_env()
        agents.append(AgentSpec("claude", "Claude Code", command, claude_env, claude=True))
    except RuntimeError:
        pass  # reported by the setup checks
    for agent_id, name, exe, args in KNOWN_AGENTS:
        found = shutil.which(exe, path=path)
        if found:
            agents.append(AgentSpec(agent_id, name, [found, *args], env))
    for entry in custom or []:
        try:
            cmd = [str(x) for x in entry["command"]]
            exe = shutil.which(cmd[0], path=path) or cmd[0]
            agents = [a for a in agents if a.id != str(entry["id"])]
            agents.append(AgentSpec(str(entry["id"]), str(entry.get("name") or entry["id"]),
                                    [exe, *cmd[1:]], dict(env, **{str(k): str(v) for k, v in
                                                                  (entry.get("env") or {}).items()})))
        except (KeyError, TypeError, IndexError):
            continue
    return agents
