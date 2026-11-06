"""Manual live check against a running iTerm2 (needs the Python API enabled and
shell integration in your shell). Opens its own window, runs a harmless failing
command, verifies detection/context/insert, then closes the window.

    .venv/bin/python tests/live_iterm_check.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import iterm2  # noqa: E402

from intelligent_iterm.terminal import ItermTerminal  # noqa: E402

MARK = "intelligent-iterm-live-check"


async def main(connection):
    app = await iterm2.async_get_app(connection)
    failures = []
    term = ItermTerminal(connection, app, failures.append)
    window = await iterm2.Window.async_create(connection)
    session = window.current_tab.current_session
    watcher = asyncio.ensure_future(term.watch())
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name, detail if not cond else "")

    try:
        await asyncio.sleep(2.5)  # let the shell start and print its prompt
        await session.async_send_text(f"ls /nonexistent-{MARK}\n")
        for _ in range(50):
            if failures:
                break
            await asyncio.sleep(0.1)
        check("failure detected", failures, "(is shell integration loaded?)")
        if failures:
            f = failures[0]
            check("command captured", f["command"] == f"ls /nonexistent-{MARK}", repr(f["command"]))
            check("exit status non-zero", f["exit"] not in (0, None), f["exit"])
            check("output captured", "No such file" in f["output"], repr(f["output"]))
            check("cwd captured", bool(f["cwd"]), f["cwd"])
            check("failure retrievable", term.get_failure(f["id"]) is f)

        ctx = await term.get_context(include_screen=True)
        check("context has cwd", bool(ctx.get("cwd")), ctx)
        check("context has last command", (ctx.get("last") or {}).get("command", "").endswith(MARK), ctx.get("last"))
        check("context has screen", MARK in (ctx.get("screen") or ""))

        # Trailing newlines are stripped (checked below: nothing executes);
        # embedded control characters are refused outright.
        for bad in ("a\rb", "echo \x1b[A", "x\x03"):
            try:
                await term.insert(bad)
                check(f"reject {bad!r}", False)
            except ValueError:
                check(f"reject {bad!r}", True)

        await term.insert(f"echo {MARK}-inserted\n")
        await asyncio.sleep(1.0)
        screen = await session.async_get_screen_contents()
        rows = [screen.line(i).string for i in range(screen.number_of_lines)]
        typed = [r for r in rows if f"echo {MARK}-inserted" in r]
        ran = [r for r in rows if r.strip() == f"{MARK}-inserted"]
        check("insert typed at prompt", typed, rows[-5:])
        check("insert NOT executed", not ran, ran)

        # Never type into a running program: Run while `sleep` runs is refused.
        await session.async_send_text("\x15sleep 3\n")
        await asyncio.sleep(0.8)
        try:
            await term.insert(f"echo {MARK}-busy", run=True)
            check("refuses to type while a command runs", False)
        except ValueError as e:
            check("refuses to type while a command runs", "sleep is still running" in str(e), str(e))
        await asyncio.sleep(3.0)
        await term.insert(f"echo {MARK}-after", run=True)
        await asyncio.sleep(1.0)
        screen = await session.async_get_screen_contents()
        rows = [screen.line(i).string for i in range(screen.number_of_lines)]
        check("types again once the prompt is back", any(r.strip() == f"{MARK}-after" for r in rows), rows[-4:])

        # Multi-line: one bracketed paste, kept in the zsh buffer until Enter.
        await session.async_send_text("\x15")
        if await term.multiline_refusal(session.session_id) is None:
            await term.insert(f"echo {MARK}-one\necho {MARK}-two")
            await asyncio.sleep(1.0)
            screen = await session.async_get_screen_contents()
            rows = [screen.line(i).string for i in range(screen.number_of_lines)]
            check("multi-line typed at prompt", any(f"echo {MARK}-two" in r for r in rows), rows[-6:])
            check("multi-line NOT executed", not any(r.strip() == f"{MARK}-one" for r in rows), rows[-6:])
            await session.async_send_text("\x03")  # ^C drops the whole buffer
            await asyncio.sleep(0.5)
            await term.insert(f"echo {MARK}-three\necho {MARK}-four", run=True)
            await asyncio.sleep(1.0)
            screen = await session.async_get_screen_contents()
            rows = [screen.line(i).string for i in range(screen.number_of_lines)]
            check("multi-line Run executes every line",
                  any(r.strip() == f"{MARK}-three" for r in rows) and any(r.strip() == f"{MARK}-four" for r in rows),
                  rows[-6:])
        else:
            print("SKIP multi-line:", await term.multiline_refusal(session.session_id))
    finally:
        watcher.cancel()
        await session.async_send_text("\x15")  # ^U clear the line before closing
        await window.async_close(force=True)
    print("ALL PASS" if ok else "SOME CHECKS FAILED")


iterm2.run_until_complete(main)
