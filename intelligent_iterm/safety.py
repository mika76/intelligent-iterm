"""Command safety: which proposed commands deserve a warning, and when a
multi-line command can be typed at the prompt without running line by line.

The risk check is a heuristic for the panel's UI, not a sandbox: it flags the
usual ways a command destroys data, escalates privileges or rewrites shared
history, so the card can say so and ask for a second click before Run.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# (pattern, what it does). Patterns run on the whole command, case-sensitive
# unless marked. Order is the order labels are shown in.
_RISKS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(^|[\s;&|(])sudo\s"), "runs as root"),
    (re.compile(r"(^|[\s;&|(])doas\s"), "runs as root"),
    (re.compile(r"(^|[\s;&|(])rm\s+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b"), "deletes files recursively"),
    (re.compile(r"(^|[\s;&|(])rm\s+(-[a-zA-Z]*f[a-zA-Z]*|--force)\b"), "deletes files without asking"),
    (re.compile(r"\bfind\b.*\s-delete\b"), "deletes files"),
    (re.compile(r"\bgit\s+push\b.*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S)"), "force-pushes (rewrites remote history)"),
    (re.compile(r"\bgit\s+reset\b.*\s--hard\b"), "discards uncommitted changes"),
    (re.compile(r"\bgit\s+clean\b.*\s-[a-zA-Z]*f"), "deletes untracked files"),
    (re.compile(r"\bgit\s+checkout\s+(--\s+)?\.(\s|$)"), "discards uncommitted changes"),
    (re.compile(r"\bgit\s+restore\b(?!.*--staged)"), "discards uncommitted changes"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "deletes a branch"),
    (re.compile(r"\bgit\s+stash\s+(drop|clear)\b"), "deletes stashed changes"),
    (re.compile(r"(^|[\s;&|(])dd\s.*\bof="), "writes raw data to a file or disk"),
    (re.compile(r"\bmkfs(\.\w+)?\b|\bdiskutil\s+(erase\w*|partitionDisk|zeroDisk)\b"), "erases a disk"),
    (re.compile(r"\b(chmod|chown)\s+(-[a-zA-Z]*R|--recursive)\b"), "changes permissions recursively"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|fi)?sh\b"), "runs a script from the internet"),
    (re.compile(r"\b(kill\s+-9|killall|pkill)\b"), "kills processes"),
    (re.compile(r"\b(shutdown|reboot|halt)\b"), "shuts down or restarts the machine"),
    (re.compile(r"\bdrop\s+(table|database|schema)\b|\btruncate\s+table\b", re.I), "deletes database data"),
    (re.compile(r"\bdocker\s+(system|volume|image|container)\s+prune\b"), "deletes Docker data"),
    (re.compile(r"\bkubectl\s+delete\b"), "deletes Kubernetes resources"),
    (re.compile(r"\bterraform\s+(destroy|apply\b.*-auto-approve)"), "changes infrastructure"),
    (re.compile(r"(^|[^<>&2])>\s*/dev/(disk|sd|nvme)"), "writes to a disk device"),
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
]


def risks(command: str) -> List[str]:
    """Plain-language warnings for a command, most important first, no duplicates."""
    out: List[str] = []
    for pattern, label in _RISKS:
        if pattern.search(command) and label not in out:
            out.append(label)
    both = ("deletes files recursively", "deletes files without asking")
    if all(b in out for b in both):  # rm -rf: one label, not two
        out[out.index(both[0])] = "deletes files recursively without asking"
        out.remove(both[1])
    return out


# Shells whose line editor turns on bracketed paste by default, so a pasted
# multi-line command lands in the buffer instead of running line by line.
# (macOS's /bin/bash 3.2 has no bracketed paste; newer bash enables it, but we
# can't tell which bash is running, so it's single-line only.)
PASTE_SAFE_SHELLS = ("zsh", "fish")


def foreground_refusal(shell: Optional[str], job: Optional[str]) -> Optional[str]:
    """None if the shell is waiting at its prompt; else why typing there now is wrong.

    Text typed while another program runs goes to that program (a REPL, an
    editor, a y/n question) or sits in the terminal's input queue, where a ^C
    can leave half of it behind."""
    shell = (shell or "").lstrip("-")
    job = (job or "").lstrip("-")
    if not shell or not job or job in (shell, "login"):
        return None
    return (f"{job} is still running in the terminal. Wait for it to finish (or press ⌃C), "
            "then click again.")


def multiline_refusal(shell: Optional[str], job: Optional[str]) -> Optional[str]:
    """None if a multi-line command can be pasted at this prompt; else why not."""
    if foreground_refusal(shell, job):
        return foreground_refusal(shell, job)
    shell = (shell or "").lstrip("-")
    if shell not in PASTE_SAFE_SHELLS:
        return (f"Multi-line commands can only be inserted in zsh or fish (this terminal runs "
                f"{shell or 'an unknown shell'}); propose a single line, joining steps with && or ;")
    return None
