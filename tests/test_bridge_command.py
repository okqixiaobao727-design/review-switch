#!/usr/bin/env python3
"""The Bridge as an installed command: what a link on PATH has to find."""

import os
import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = REPO_ROOT / "bridge" / "review_bridge.py"
README = REPO_ROOT / "README.md"
COMMAND_NAME = "review-bridge"


class BridgeCommandTests(unittest.TestCase):
    def test_the_bridge_is_executable(self):
        self.assertTrue(BRIDGE.is_file(), f"{BRIDGE} is missing")
        self.assertTrue(
            os.access(BRIDGE, os.X_OK),
            f"{BRIDGE} is linked onto PATH and must be executable",
        )

    def test_the_bridge_names_its_own_interpreter(self):
        first_line = BRIDGE.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first_line, "#!/usr/bin/env python3")

    def test_the_readme_installs_the_command_alongside_the_skill(self):
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            'mkdir -p "$HOME/.claude/skills" "$HOME/.local/bin"',
            readme,
            "a link into a directory that does not exist yet fails",
        )
        self.assertIn('ln -sfn "$PWD/skills/review-switch"', readme)
        self.assertIn(
            f'ln -sfn "$PWD/bridge/review_bridge.py" "$HOME/.local/bin/{COMMAND_NAME}"',
            readme,
        )

    def test_the_readme_runs_the_command_a_terminal_runs(self):
        readme = README.read_text(encoding="utf-8")
        self.assertIn(f"{COMMAND_NAME} --reviewer", readme)


if __name__ == "__main__":
    unittest.main()
