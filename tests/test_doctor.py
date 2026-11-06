"""Setup checks: each one's verdict and fix text, without touching the real machine."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from intelligent_iterm import doctor  # noqa: E402


class DoctorTest(unittest.TestCase):
    def test_node(self):
        self.assertEqual(doctor.check_node(None, None)["status"], "fail")
        old = doctor.check_node("/usr/bin/node", "v18.19.0")
        self.assertEqual(old["status"], "fail")
        self.assertIn("22", old["fix"])
        self.assertEqual(doctor.check_node("/usr/bin/node", "v22.11.0")["status"], "ok")
        self.assertEqual(doctor.check_node("/usr/bin/node", "v24.1.0")["status"], "ok")

    def test_adapter(self):
        self.assertEqual(doctor.check_adapter(Path("/nonexistent/index.js"))["status"], "fail")
        self.assertEqual(doctor.check_adapter(Path(__file__))["status"], "ok")

    def test_login(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / ".claude.json"
            self.assertEqual(doctor.check_login(cfg, {})["status"], "warn")  # no file
            cfg.write_text(json.dumps({"oauthAccount": {"x": 1}}))
            self.assertEqual(doctor.check_login(cfg, {})["status"], "ok")
            cfg.write_text("{}")
            self.assertEqual(doctor.check_login(cfg, {"CLAUDE_CODE_OAUTH_TOKEN": "t"})["status"], "ok")
            # An API key only counts when the panel is told to keep it.
            self.assertEqual(doctor.check_login(cfg, {"ANTHROPIC_API_KEY": "k"})["status"], "warn")
            self.assertEqual(doctor.check_login(cfg, {"ANTHROPIC_API_KEY": "k",
                                                      "INTELLIGENT_ITERM_KEEP_API_KEY": "1"})["status"], "ok")

    def test_shell_integration(self):
        self.assertEqual(doctor.check_shell_integration(True)["status"], "ok")
        self.assertEqual(doctor.check_shell_integration(False)["status"], "warn")
        self.assertEqual(doctor.check_shell_integration(None)["status"], "warn")

    def test_report(self):
        checks = [doctor.check("A", "ok", "fine", "unused"), doctor.check("B", "fail", "broken", "do X")]
        report = doctor.format_report(checks)
        self.assertIn("✓ A: fine", report)
        self.assertIn("✗ B: broken", report)
        self.assertIn("→ do X", report)
        self.assertNotIn("unused", report)
        self.assertEqual([c["name"] for c in doctor.problems(checks)], ["B"])


if __name__ == "__main__":
    unittest.main()
