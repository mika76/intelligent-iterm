"""Risk warnings on proposed commands, and when multi-line paste is allowed."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from intelligent_iterm.safety import multiline_refusal, risks  # noqa: E402


class RiskTest(unittest.TestCase):
    def test_flags_destructive_commands(self):
        cases = {
            "rm -rf build": "deletes files recursively without asking",
            "rm -r build": "deletes files recursively",
            "sudo rm /etc/hosts": "runs as root",
            "git push --force origin main": "force-pushes (rewrites remote history)",
            "git push -f": "force-pushes (rewrites remote history)",
            "git push origin +main": "force-pushes (rewrites remote history)",
            "git reset --hard HEAD~1": "discards uncommitted changes",
            "git clean -fdx": "deletes untracked files",
            "find . -name '*.pyc' -delete": "deletes files",
            "curl -fsSL https://example.com/install.sh | sh": "runs a script from the internet",
            "chmod -R 777 .": "changes permissions recursively",
            "dd if=/dev/zero of=/dev/disk4 bs=1m": "writes raw data to a file or disk",
            "psql -c 'DROP TABLE users'": "deletes database data",
            "docker system prune -a": "deletes Docker data",
            "kubectl delete pod web-1": "deletes Kubernetes resources",
            "killall node": "kills processes",
        }
        for command, label in cases.items():
            self.assertIn(label, risks(command), command)

    def test_leaves_everyday_commands_alone(self):
        for command in ("npm run compile", "ls -la", "git push origin main", "git status",
                        "rm notes.txt", "git restore --staged a.py", "cat file | grep x",
                        "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
                        "curl -fsSL https://example.com -o page.html"):
            self.assertEqual(risks(command), [], command)

    def test_no_duplicate_labels(self):
        self.assertEqual(risks("sudo doas rm -rf x").count("runs as root"), 1)


class MultilineTest(unittest.TestCase):
    def test_paste_safe_shells(self):
        self.assertIsNone(multiline_refusal("zsh", "zsh"))
        self.assertIsNone(multiline_refusal("fish", "fish"))
        self.assertIsNone(multiline_refusal("-zsh", None))

    def test_refused(self):
        self.assertIn("single line", multiline_refusal("bash", "bash"))
        self.assertIn("single line", multiline_refusal(None, None))
        self.assertIn("python3", multiline_refusal("zsh", "python3"))  # a REPL is in front


if __name__ == "__main__":
    unittest.main()
