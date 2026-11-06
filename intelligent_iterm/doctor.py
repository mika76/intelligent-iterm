"""Setup checks: catch the usual reasons the panel doesn't work, and say how to fix each.

Run from a shell (install.sh does this at the end):

    python3 -m intelligent_iterm.doctor

The panel runs the same checks when it opens, plus the ones that need iTerm2
(shell integration in the active session), and shows any problems; /doctor in
the panel shows the full report.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import config

MIN_NODE = 22
OK, WARN, FAIL = "ok", "warn", "fail"


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def check(name: str, status: str, detail: str, fix: str = "") -> Dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def node_version(node: str) -> Optional[str]:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def check_node(node: Optional[str], version: Optional[str]) -> Dict[str, str]:
    name = "Node.js"
    if not node:
        return check(name, FAIL, "node not found (looked on PATH, in nvm, Homebrew and /usr/local)",
                     f"Install Node.js {MIN_NODE} or newer, or set INTELLIGENT_ITERM_NODE to its path.")
    if not version:
        return check(name, FAIL, f"{node} didn't report a version", "Check that this node runs.")
    try:
        major = int(version.lstrip("v").split(".")[0])
    except ValueError:
        return check(name, WARN, f"{node}: unrecognised version {version!r}")
    if major < MIN_NODE:
        return check(name, FAIL, f"{node} is {version}",
                     f"Node.js {MIN_NODE} or newer is required. Install it (e.g. `nvm install {MIN_NODE}`) "
                     "or point INTELLIGENT_ITERM_NODE at a newer node.")
    return check(name, OK, f"{version} ({tilde(node)})")


def check_adapter(adapter: Path) -> Dict[str, str]:
    if adapter.exists():
        return check("Claude Code ACP adapter", OK, "installed")
    return check("Claude Code ACP adapter", FAIL, "not installed",
                 f"Run ./install.sh in {config.ROOT}.")


def check_claude_cli(claude: Optional[str]) -> Dict[str, str]:
    if claude:
        return check("Claude Code CLI", OK, tilde(claude))
    return check("Claude Code CLI", WARN, "`claude` not found; the adapter's bundled copy will be used",
                 "Install Claude Code, or set INTELLIGENT_ITERM_CLAUDE to its path.")


def check_login(claude_json: Path, env: Dict[str, str]) -> Dict[str, str]:
    """Best effort: Claude Code records the signed-in account in ~/.claude.json."""
    name = "Claude Code login"
    try:
        data = json.loads(claude_json.read_text())
    except (OSError, ValueError):
        data = {}
    if data.get("oauthAccount"):
        return check(name, OK, "signed in")
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return check(name, OK, "CLAUDE_CODE_OAUTH_TOKEN is set")
    if env.get("ANTHROPIC_API_KEY") and env.get("INTELLIGENT_ITERM_KEEP_API_KEY") == "1":
        return check(name, OK, "using ANTHROPIC_API_KEY")
    return check(name, WARN, "couldn't find a Claude Code login",
                 "Run `claude` in a terminal and sign in with /login.")


def check_agents(names: List[str]) -> Dict[str, str]:
    others = [n for n in names if n != "Claude Code"]
    if not others:
        return check("Other agents", OK, "none found (Gemini CLI, OpenCode and GitHub Copilot are "
                     "picked up when installed; others can be added in settings.json)")
    return check("Other agents", OK, ", ".join(others) + " (pick one in the panel header)")


def basic_checks(env: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
    """Everything that doesn't need iTerm2."""
    env = dict(os.environ) if env is None else env
    node = config.find_node()
    return [
        check_node(node, node_version(node) if node else None),
        check_adapter(config.ADAPTER),
        check_claude_cli(config.find_claude()),
        check_login(Path(os.path.expanduser("~/.claude.json")), env),
        check_agents([a.name for a in config.available_agents(_custom_agents())]),
    ]


def _custom_agents() -> List[Dict[str, Any]]:
    from .settings import DEFAULT_PATH, Settings
    return Settings(DEFAULT_PATH).custom_agents()


def check_shell_integration(has_prompts: Optional[bool]) -> Dict[str, str]:
    name = "Shell integration"
    if has_prompts is None:
        return check(name, WARN, "no terminal session to check")
    if has_prompts:
        return check(name, OK, "active in this session")
    return check(name, WARN, "not active in this session, so failed commands aren't detected",
                 "iTerm2 > Install Shell Integration, then open a new tab.")


async def all_checks(shell_integration: Optional[Callable[[], Awaitable[Optional[bool]]]] = None
                     ) -> List[Dict[str, str]]:
    import asyncio
    checks = await asyncio.get_running_loop().run_in_executor(None, basic_checks)
    if shell_integration is not None:
        try:
            checks.append(check_shell_integration(await shell_integration()))
        except Exception as e:  # noqa: BLE001 - a check must never break the panel
            checks.append(check("Shell integration", WARN, f"couldn't check ({e})"))
    return checks


def problems(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for c in checks if c["status"] != OK]


def format_report(checks: List[Dict[str, str]]) -> str:
    mark = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = []
    for c in checks:
        lines.append(f"{mark[c['status']]} {c['name']}: {c['detail']}")
        if c["fix"] and c["status"] != OK:
            lines.append(f"    → {c['fix']}")
    return "\n".join(lines)


def main() -> int:
    checks = basic_checks()
    print(format_report(checks))
    print("\nIn iTerm2, also make sure: Settings > General > Magic > Enable Python API is on,\n"
          "Scripts > Manage > Install Python Runtime is done, and shell integration is installed.\n"
          "Type /doctor in the panel for the checks that need iTerm2.")
    return 1 if any(c["status"] == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
