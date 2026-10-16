"""No test can reach the real `claude` by leaving cli_path empty: the PATH lookup is blocked unless a run asks for it."""
import shutil
import unittest

import assistant


class NoRealClaudeTests(unittest.TestCase):
    def test_an_empty_cli_path_finds_no_claude_on_path(self):
        self.assertIsNone(assistant.resolve_cli({}))
        self.assertIsNone(assistant.resolve_cli({"cli_path": ""}))

    def test_other_programs_are_still_found(self):
        self.assertIsNotNone(shutil.which("cmd") or shutil.which("sh"))


if __name__ == "__main__":
    unittest.main()
