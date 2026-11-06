# Changelog

Notable changes to intelligent-iterm. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Each release's upgrade steps are in its
[GitHub release](https://github.com/mika76/intelligent-iterm/releases).

## [Unreleased]

## [0.3.0] - 2026-11-06

### Added
- Other ACP agents: Gemini CLI, OpenCode and GitHub Copilot when they're installed, or a custom agent. Each tab picks its own agent, and the choice becomes the default for new tabs.
- Settings menu: click the agent's name, or type `/settings`, for the agent, model, mode, effort and context. Model, mode and effort apply to every tab using that agent and are remembered across runs. Bypass permissions isn't offered.
- Cards by keyboard: ↑ from an empty input selects the last card, ←/→ move between its buttons, Enter presses one, Esc goes back. Permission cards also take Y and N.
- Command cards warn about destructive commands and ask for confirmation before Run.
- **What does this do?** on command cards asks for an explanation; the agent then proposes again.
- Multi-line commands are inserted as a bracketed paste in zsh and fish.

### Changed
- Minimal header: the tab's agent only, plus the model when it isn't the default and a badge when the mode differs.
- The panel is named after its agent. The docked pane's title follows the tab's agent, the toolbelt entry is now **View > Toolbelt > Agent**, and the status bar button (**Agent panel**) shows ⬓ with the tab's agent.
- Typing or pasting anywhere in the panel goes to the input, even after clicking a message.
- A new docked panel loads behind the maximized terminal and appears once its page has connected, with no white flash or strip.
- The docked panel's URL bar is hidden with iTerm2's `browser_set_toolbar_hidden` where available.
- New demo GIF and screenshot in the README.

### Fixed
- Insert and Run only type at a waiting shell prompt and refuse while a command is running. Previously Run could leave text like `ind …` behind.
- The status bar button keeps working after an update, because iTerm2 calls the component with the signature it stored.
- A closed docked panel is forgotten even when iTerm2's close notice arrives late or not at all.
- Sending a message scrolls to the bottom, and the log keeps following as cards grow.

## [0.2.0] - 2026-11-06

### Added
- One conversation per tab: each iTerm2 tab has its own conversation and Claude Code process. A tab's docked panel shows its own conversation, and the toolbelt follows the active tab. Closing a tab stops its agent.
- Setup checks when the panel opens: Node, the adapter, the Claude Code login and shell integration. `/doctor` shows the full report, `python3 -m intelligent_iterm.doctor` runs it from a shell, and `install.sh` runs it at the end.
- Demo GIF in the README.
- CI runs the unit tests on Linux and macOS.

### Changed
- A command that fails in a background tab shows its Explain bar in that tab's panel instead of being dropped.

### Fixed
- Text written after a command, permission or setup card starts a new message below the card instead of being appended above it.
- Explain clears the failure bar in every view of the conversation, and it stays cleared after a reload.

## [0.1.0] - 2026-11-06

### Added
- First release: a Claude Code panel for iTerm2, docked below the terminal or in the toolbelt. It explains failed commands, proposes fixes as Insert/Run cards, and reads terminal context.

[Unreleased]: https://github.com/mika76/intelligent-iterm/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/mika76/intelligent-iterm/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/mika76/intelligent-iterm/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mika76/intelligent-iterm/releases/tag/v0.1.0
