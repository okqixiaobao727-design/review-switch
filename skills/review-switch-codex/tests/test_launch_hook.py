#!/usr/bin/env python3
"""Tests for launch_hook.py, the on-child-launch hook both bridges launch through.

The hook is the plugin's one extension point at a child launch: a command and a
set of child environment variables, both empty until a project configures them.
Every test here runs the real thing — a real config file, a real shell command —
so what it pins is the behaviour a user configuring the hook gets.
"""

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from hook_fixtures import marker_lines, write_hook_config  # noqa: E402


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "launch_hook.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("launch_hook", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_config(root, body):
    """A config written out whole, for the shapes the hook must survive."""
    (pathlib.Path(root) / "agentcrew.toml").write_text(body, encoding="utf-8")


APPEND_CONTEXT = (
    'printf "%s\\t%s\\n" "$AGENTCREW_CHILD_CWD" "$AGENTCREW_CHILD_TMUX_TARGET" >>'
)


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.hooks = load_module()
        self.work = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.work.name)
        self.marker = self.root / "launched.log"
        self.addCleanup(self.work.cleanup)

    def marker_lines(self):
        return marker_lines(self.marker)


class LoadTests(HookTestCase):
    def test_a_tree_without_a_config_carries_no_hook(self):
        hook = self.hooks.load_hook(self.root)

        self.assertEqual(hook.command, "")
        self.assertEqual(hook.env, {})

    def test_the_hook_is_read_from_the_nearest_config_above_the_child(self):
        write_config(
            self.root,
            '[hooks.on-child-launch]\ncommand = "true"\n'
            '[hooks.on-child-launch.env]\nCREW_LAUNCH_TAG = "ticket-133"\n',
        )
        worktree = self.root / ".claude" / "worktrees" / "01-slug"
        worktree.mkdir(parents=True)

        hook = self.hooks.load_hook(worktree)

        self.assertEqual(hook.command, "true")
        self.assertEqual(hook.env, {"CREW_LAUNCH_TAG": "ticket-133"})

    def test_a_config_without_the_hook_section_carries_no_hook(self):
        write_config(self.root, '[implementer.direct.any]\nexecutor = "claude"\n')

        self.assertEqual(self.hooks.load_hook(self.root).command, "")

    def test_an_unparsable_config_carries_no_hook(self):
        write_config(self.root, "[hooks.on-child-launch\ncommand =\n")

        self.assertEqual(self.hooks.load_hook(self.root).command, "")


class ChildEnvironmentTests(HookTestCase):
    def test_a_child_of_an_unconfigured_hook_gets_no_extra_environment(self):
        self.assertEqual(self.hooks.load_hook(self.root).child_env(), dict(os.environ))

    def test_the_configured_variables_reach_the_child(self):
        write_config(
            self.root,
            "[hooks.on-child-launch.env]\n"
            'CREW_LAUNCH_TAG = "ticket-133"\nCREW_LAUNCH_LANE = "review"\n',
        )

        env = self.hooks.load_hook(self.root).child_env()

        self.assertEqual(env["CREW_LAUNCH_TAG"], "ticket-133")
        self.assertEqual(env["CREW_LAUNCH_LANE"], "review")

    def test_the_configured_variables_win_over_the_inherited_ones(self):
        write_config(
            self.root,
            '[hooks.on-child-launch.env]\nCREW_LAUNCH_TAG = "from-config"\n',
        )

        env = self.hooks.load_hook(self.root).child_env(
            {"CREW_LAUNCH_TAG": "from-the-caller"}
        )

        self.assertEqual(env["CREW_LAUNCH_TAG"], "from-config")


class RunTests(HookTestCase):
    def test_an_unconfigured_hook_calls_nothing(self):
        self.hooks.load_hook(self.root).run(self.root, tmux_target="%42")

        self.assertEqual(self.marker_lines(), [])

    def test_the_command_runs_once_per_launch_and_is_told_which_child(self):
        write_hook_config(self.root, command=f"{APPEND_CONTEXT} {self.marker}")
        hook = self.hooks.load_hook(self.root)

        hook.run(self.root, tmux_target="%42")

        self.assertEqual(self.marker_lines(), [f"{self.root}\t%42"])

    def test_a_child_launched_outside_tmux_leaves_the_target_empty(self):
        write_hook_config(self.root, command=f"{APPEND_CONTEXT} {self.marker}")

        self.hooks.load_hook(self.root).run(self.root)

        self.assertEqual(self.marker_lines(), [f"{self.root}\t"])

    def test_the_command_is_handed_the_child_variables_too(self):
        write_hook_config(
            self.root,
            command=f'printf "%s\\n" "$CREW_LAUNCH_TAG" >> {self.marker}',
            env={"CREW_LAUNCH_TAG": "ticket-133"},
        )

        self.hooks.load_hook(self.root).run(self.root)

        self.assertEqual(self.marker_lines(), ["ticket-133"])

    def test_a_failing_command_leaves_the_launch_standing(self):
        write_hook_config(
            self.root, command=f"echo failing >> {self.marker}; exit 3"
        )

        self.hooks.load_hook(self.root).run(self.root)

        self.assertEqual(self.marker_lines(), ["failing"])

    def test_a_command_that_cannot_be_found_leaves_the_launch_standing(self):
        write_hook_config(self.root, command="agentcrew-hook-that-is-not-installed")

        self.hooks.load_hook(self.root).run(self.root)


if __name__ == "__main__":
    unittest.main()
