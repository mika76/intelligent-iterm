# intelligent-iterm

A Claude side panel for iTerm2: a toolbelt pane that can see your terminal,
explain failed commands, and suggest commands you can insert at the prompt.
Inspired by Microsoft's Intelligent Terminal. MVP quality.

```
iTerm2 toolbelt (webview) ──HTTP/SSE──▶ intelligent_iterm (Python, iTerm2 API script)
                                           │  reads sessions, watches prompts, types text
                                           └─stdio JSON-RPC (ACP)─▶ claude-agent-acp ─▶ Claude Code
```

- **Agent:** Claude Code via [`@agentclientprotocol/claude-agent-acp`](https://github.com/agentclientprotocol/claude-agent-acp)
  (the renamed successor of `@zed-industries/claude-code-acp`), pinned in `package.json`.
  It uses your existing Claude Code login. No keys are stored here.
- **Script:** stdlib + the `iterm2` package only, so it runs on iTerm2's basic Python runtime.

## Features

1. **Chat panel** with streamed replies, in either of two places:
   - **Docked below the terminal** (like Intelligent Terminal's agent pane). Open or close
     it for the current tab with **Ctrl+Shift+.**, the **✳ Claude ▴/▾** status bar button,
     `hide` in the panel, or `dock ↓` in the toolbelt. To add the button, go to iTerm2 >
     Settings > Profiles > Session > *Configure Status Bar* and drag in **Claude panel**.
     It only appears in the picker while the script is running. It's an iTerm2 browser split pane, created with per-session settings,
     so no profile is added to your iTerm2 config. The script hides the pane's browser
     URL bar by pressing **Web > Show Toolbar** through macOS accessibility. iTerm2 has no
     setting or API for this. The first time, macOS may ask to let iTerm2 control
     System Events and use Accessibility. If you decline, the URL bar stays; you can hide it
     yourself with Web > Show Toolbar, or set `INTELLIGENT_ITERM_KEEP_BROWSER_TOOLBAR=1`
     to skip the step.
   - **In the toolbelt** on the right (View > Toolbelt > Claude).

   Both show the same conversation.
2. **Attach context** (on by default) sends the active session's directory, last command,
   exit status and output with each message. If shell integration has no command
   history, it sends the last 60 lines of the terminal instead. **+ screen** also attaches
   the visible screen once, with the next message.
3. **Explain this error**: when a command in the active session exits non-zero, a status
   bar above the input shows it with an **Explain** button, which sends the command, exit
   status and output to Claude.
4. **Insert**: each single-line command that Claude suggests in a `bash` block gets an
   Insert button. It types the command at the prompt of the active session and **never
   presses Enter**. Text containing newlines or control characters is refused, both in the
   UI and on the server, so multi-line suggestions only offer Copy.

The panel takes its colors and font from the active session's iTerm2 profile, and updates
when you switch to a session with a different profile, so it looks like part of the terminal.

Claude's own tool use (reading files, running commands in its own process) runs in
Claude Code's **Manual** permission mode. Each tool call shows Allow/Reject buttons in the panel.

## Requirements

- A recent iTerm2 (only tested on 3.7.7; prompt IDs and command-end notifications need a modern build)
- **Python API enabled:** iTerm2 > Settings > General > Magic > *Enable Python API*
- **Shell integration** installed (iTerm2 > Install Shell Integration). It is needed for
  failed-command detection and per-command output.
- **iTerm2 Python runtime:** Scripts > Manage > *Install Python Runtime*
- Node.js ≥ 22 (nvm, Homebrew or /usr/local are all found automatically)
- Claude Code installed and logged in (`claude` once, then `/login`)

## Install

```bash
./install.sh
```

This runs `npm ci` for the adapter and writes a small launcher to
`~/Library/Application Support/iTerm2/Scripts/AutoLaunch/intelligent_iterm_panel.py`.
The launcher points back at this folder, so keep the folder where it is.

Then in iTerm2:

1. **Scripts > AutoLaunch > intelligent_iterm_panel.py** to start it now (it also starts
   automatically whenever iTerm2 launches).
2. **View > Toolbelt > Claude** to add the panel to the toolbelt.
3. **View > Toolbelt > Show Toolbelt** (⇧⌘B) to show it.

To remove it: `./install.sh uninstall`.

Logs go to `~/Library/Logs/intelligent-iterm.log`. iTerm2's Scripts > Manage > Console
also shows the script's output.

### Configuration (environment variables, all optional)

| Variable | Purpose |
|---|---|
| `INTELLIGENT_ITERM_NODE` | Path to `node` if auto-detection fails |
| `INTELLIGENT_ITERM_CLAUDE` | Path to the `claude` CLI to use |
| `INTELLIGENT_ITERM_KEEP_BROWSER_TOOLBAR=1` | Leave the URL bar visible in the docked pane (skips the accessibility click) |
| `INTELLIGENT_ITERM_PORT` | Fixed port for the local server (default: random) |
| `INTELLIGENT_ITERM_KEEP_API_KEY=1` | Keep `ANTHROPIC_API_KEY` in the agent's environment. By default it is removed so the panel uses your Claude Code login rather than billing an API key. |

## Development

```bash
python3 -m unittest discover -s tests        # bridge tests with a fake agent (no tokens)
```

```bash
python3 tests/dev_server.py                  # UI in a normal browser at http://127.0.0.1:8765/?t=dev
```

```bash
python3 tests/dev_server.py --real           # same, with real Claude Code
```

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python tests/live_iterm_check.py   # live check: opens an iTerm2 window, runs a failing ls, closes it
```

```bash
.venv/bin/python tests/live_dock_check.py    # live check: docks the panel in its own window, then closes it
```

```bash
.venv/bin/python -m intelligent_iterm.main   # run the panel from a shell instead of AutoLaunch
```

| File | What it does |
|---|---|
| `intelligent_iterm/main.py` | Entry point: starts the server, registers the toolbelt tool, watches sessions |
| `intelligent_iterm/dock.py` | Bottom-docked browser split and the Ctrl+Shift+. shortcut |
| `intelligent_iterm/terminal.py` | iTerm2 API: context, failed-command detection, safe insert |
| `intelligent_iterm/panel.py` | Turns UI actions into ACP prompts and agent updates into UI events |
| `intelligent_iterm/acp.py` | Minimal ACP (JSON-RPC over stdio) client |
| `intelligent_iterm/web.py` | Local HTTP + Server-Sent Events server |
| `intelligent_iterm/config.py` | Finds node, claude and the adapter outside your shell's PATH |
| `intelligent_iterm/static/index.html` | The chat UI (no build step) |

## Security notes

- The server listens on 127.0.0.1 only. Every API call and the event stream require a
  random token generated at each launch, and requests with a foreign `Host` header are
  rejected. Without this, any web page in your browser could make the panel type into
  your terminal.
- Insert never sends Enter, newlines, or control characters.

## Known limitations

- **Docking:** the toolbelt only goes on the right. The bottom dock splits the *active pane*,
  not the whole tab, because the API can't split a tab's root. With several panes in a tab
  it sits under the one you were in. Its size is set once, to about 35% of that pane; drag
  the divider to change it. The dock depends on iTerm2's browser profiles (tested on 3.7.7; the
  `browserProfiles` advanced setting, on by default).
- **The docked panel grays out when it isn't focused.** iTerm2 dims inactive browser panes
  with a gray overlay and ignores "Dimming affects only text" for them. Only the global
  settings under Settings > Appearance > Dimming change this: lower *Dimming amount*, or
  untick *Dim inactive split panes*.
- **Ctrl+Shift+.** is captured from every terminal session while the script runs, so
  programs in the terminal no longer receive it.

- **One conversation, one agent process.** It starts in the active session's directory
  when you send the first message. "New chat" starts a fresh one, for example after you
  `cd` elsewhere.
- **"Active session"** means the focused terminal in the frontmost iTerm2 window. When you
  are typing in the docked panel, it means the terminal you last used in that tab. Insert
  goes there and focuses it, which is not necessarily the session the failure came from.
- **Failure cards** appear only for commands that end while their session is active, and
  only with shell integration (OSC 133 marks). Commands that ran before the script started
  are still used for context, via prompt history.
- **Output capture** is limited to the last 200 lines of a command, and to whatever is
  still in scrollback. Full-screen apps (vim, less, top) don't produce usable output.
- **Conversation history** lives in memory. Reloading the panel replays it, but
  restarting the script or iTerm2 clears it. Sessions are not resumed (`session/load`).
- **Markdown rendering** is minimal: paragraphs, code blocks, `inline code` and **bold**.
- **Permission prompts** only offer the options the adapter sends. There is no per-tool
  settings UI. The session runs in Manual mode regardless of your Claude Code default.
- **First message cost:** Claude Code loads your usual configuration (CLAUDE.md, skills,
  MCP servers), so the first turn has a sizeable cached prompt.
- **Multiple iTerm2 windows** share one panel conversation.
