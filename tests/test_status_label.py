"""The status bar button's label, read from its state variable."""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@unittest.skipUnless(importlib.util.find_spec("iterm2"), "needs the iterm2 package")
class StatusLabelTest(unittest.TestCase):
    def test_label(self):
        from intelligent_iterm.dock import status_label
        self.assertEqual(status_label("open|GitHub Copilot", "Claude Code"), "⬓ GitHub Copilot ▾")
        self.assertEqual(status_label("closed|Claude Code", "x"), "⬓ Claude Code ▴")
        self.assertEqual(status_label(None, "Claude Code"), "⬓ Claude Code ▴")
        self.assertEqual(status_label("open", "Claude Code"), "⬓ Claude Code ▾")  # set by an older copy


if __name__ == "__main__":
    unittest.main()
