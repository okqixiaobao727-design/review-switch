#!/usr/bin/env python3
"""Behaviour of the repository residue lint at its CLI seam."""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_plugin_tree.py"
IDENTIFIERS_FILE = ".review-switch-local-identifiers"
PRIVATE_APP_NAME = "ChatGPT " + "Hands" + "-Free"
PRIVATE_HOME_PATH = "$" + "HOME/Library/Application Support/"
BRIDGE_COMMAND = "bridge" + "ctl"
PRIVATE_ENV_TOKEN = "HANDS" + "_FREE_CHILD_SESSION"
SPEND_MONTH = "$" + "20" + "/month"
STRUCTURED_SPEND = "total_" + "cost_usd"
FIXTURE_IDENTIFIER = "fixture" + "-host"


def run_validator(root, *, from_root=False):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT if from_root else None,
        env=os.environ.copy(),
    )


class TreeFixture:
    """A writable copy of the canonical tree for one residue scenario."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name) / "review-switch"
        shutil.copytree(REPO_ROOT, self.root, ignore=shutil.ignore_patterns(".git"))
        self.path(IDENTIFIERS_FILE).unlink(missing_ok=True)

    def close(self):
        self._tmp.cleanup()

    def path(self, relative):
        return self.root / relative

    def write(self, relative, text):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit_as_checkout(self):
        """Make the copy a checkout, tracking everything written so far.

        The copy arrives without a `.git`, so by default it is the exported
        tree the lint falls back to walking. A test that needs the lint to
        have a checkout to ask says so by calling this.
        """
        for arguments in (
            ("init", "-q"),
            ("config", "user.email", "lint@example.invalid"),
            ("config", "user.name", "Residue Lint Fixture"),
            ("add", "-A"),
            ("commit", "-qm", "the tree as it ships"),
        ):
            subprocess.run(
                ("git", "-C", str(self.root)) + arguments,
                check=True,
                capture_output=True,
            )


class ValidatePluginTreeTests(unittest.TestCase):
    def setUp(self):
        self.tree = TreeFixture()
        self.addCleanup(self.tree.close)

    def assert_rejects(self, text, mentioning):
        self.tree.write("bridge/tests/residue-fixture.txt", text)
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(mentioning, result.stdout + result.stderr)

    def test_clean_tree_passes_without_local_identifiers_file(self):
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_contract_command_passes_from_the_repository_root(self):
        result = run_validator(REPO_ROOT, from_root=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_private_bridge_path_is_rejected(self):
        self.assert_rejects(
            f"{PRIVATE_HOME_PATH}{PRIVATE_APP_NAME}/runtime/{BRIDGE_COMMAND}\n",
            PRIVATE_APP_NAME,
        )

    def test_private_environment_token_is_rejected(self):
        self.assert_rejects(f"{PRIVATE_ENV_TOKEN}=1\n", PRIVATE_ENV_TOKEN)

    def test_spend_figure_is_rejected(self):
        self.assert_rejects(f"The plan costs {SPEND_MONTH}.\n", "spend figure")

    def test_structured_spend_figure_is_rejected(self):
        self.assert_rejects(f"{STRUCTURED_SPEND}: 0.12\n", "spend figure")

    def test_plain_currency_amount_is_rejected(self):
        self.assert_rejects("The fee is " + "$" + "20.\n", "spend figure")

    def test_comma_grouped_spend_figure_is_rejected(self):
        self.assert_rejects("We spent $" + "1,200 on the run.\n", "spend figure")

    def test_comma_grouped_spend_figure_with_currency_is_rejected(self):
        self.assert_rejects("The budget is " + "1,200 " + "USD.\n", "spend figure")

    def test_non_monetary_numbers_are_accepted(self):
        self.tree.write(
            "bridge/tests/residue-fixture.txt",
            "Retry until the amount of pending waves reaches 0.\n"
            "The reviewer costs about 3 rounds of context.\n"
            "Set the price of admission: 2 approvals.\n"
            "Use $1 and '$3:' as shell parameters.\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_configured_identifier_is_rejected(self):
        self.tree.write(IDENTIFIERS_FILE, f"{FIXTURE_IDENTIFIER}\n")
        self.assert_rejects(
            f"ssh {FIXTURE_IDENTIFIER} hostname\n", FIXTURE_IDENTIFIER
        )

    def test_identifier_rule_is_inert_without_local_file(self):
        self.tree.write(
            "bridge/tests/residue-fixture.txt",
            f"ssh {FIXTURE_IDENTIFIER} hostname\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_local_identifiers_file_is_not_scanned_as_residue(self):
        self.tree.write(IDENTIFIERS_FILE, f"{FIXTURE_IDENTIFIER}\n")
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nested_shipped_tests_directory_is_scanned(self):
        self.tree.write(
            "bridge/tests/residue-fixture.txt",
            f"{PRIVATE_ENV_TOKEN}\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("residue-fixture.txt", result.stdout + result.stderr)

    def test_top_level_shipped_tests_directory_is_scanned(self):
        self.tree.write("tests/residue-fixture.txt", f"{PRIVATE_ENV_TOKEN}\n")
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("tests/residue-fixture.txt", result.stdout + result.stderr)

    def test_repository_artifacts_are_not_scanned(self):
        self.tree.write(IDENTIFIERS_FILE, f"{FIXTURE_IDENTIFIER}\n")
        for directory in (
            ".git",
            ".pytest_cache",
            ".code-review-graph",
            ".venv",
            "build",
            "dist",
        ):
            self.tree.write(f"{directory}/private.txt", f"{FIXTURE_IDENTIFIER}\n")
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write_private_residue(self, relative):
        """Put a private identifier in one file, and arm the rule that catches it."""
        self.tree.write(IDENTIFIERS_FILE, f"{FIXTURE_IDENTIFIER}\n")
        self.tree.write(
            relative,
            f"gitdir: /Users/{FIXTURE_IDENTIFIER}/repo/.git/worktrees/other\n",
        )

    def test_a_worktree_another_session_left_does_not_fail_a_checkout(self):
        """The shape that failed the lint on `main` whenever a worktree existed.

        A linked worktree's `.git` is a file naming an absolute path, so it
        carries the developer's user name. Nothing tracks it and no directory
        name in `.gitignore` covers it, so only asking git keeps it out.
        """
        self.tree.commit_as_checkout()
        self.write_private_residue(".claude/worktrees/another-session/.git")

        result = run_validator(self.tree.root)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_one_file_passes_untracked_and_fails_once_tracked(self):
        """Tracking is the whole difference: same path, same bytes, both ways."""
        residue = "docs/agents/residue-fixture.md"
        self.tree.commit_as_checkout()
        self.write_private_residue(residue)

        untracked = run_validator(self.tree.root)
        self.tree.commit_as_checkout()
        tracked = run_validator(self.tree.root)

        self.assertEqual(untracked.returncode, 0, untracked.stderr)
        self.assertEqual(tracked.returncode, 1, tracked.stdout + tracked.stderr)
        self.assertIn(residue, tracked.stdout + tracked.stderr)

    def test_a_tree_with_no_checkout_still_scans_what_lies_in_it(self):
        """The export case: with no git to ask, the walk is all there is."""
        self.write_private_residue(".claude/worktrees/another-session/.git")

        result = run_validator(self.tree.root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(FIXTURE_IDENTIFIER, result.stdout + result.stderr)

    def test_an_export_inside_a_checkout_is_walked_not_answered_for(self):
        """git searches upwards, so a tree with no checkout of its own has one.

        An export unpacked anywhere inside a repository would otherwise be
        answered for by its host, whose index says nothing about it — the lint
        would scan nothing and pass. Only the tree that *is* the checkout may
        answer with git.
        """
        self.tree.commit_as_checkout()
        self.tree.write(f"export/{IDENTIFIERS_FILE}", f"{FIXTURE_IDENTIFIER}\n")
        self.tree.write(
            "export/notes.md",
            f"gitdir: /Users/{FIXTURE_IDENTIFIER}/repo/.git\n",
        )

        result = run_validator(self.tree.path("export"))

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(FIXTURE_IDENTIFIER, result.stdout + result.stderr)

    def test_configured_identifier_matches_whole_tokens_only(self):
        self.tree.write(IDENTIFIERS_FILE, f"{FIXTURE_IDENTIFIER}\n")
        self.tree.write(
            "bridge/tests/residue-fixture.txt",
            f"ssh {FIXTURE_IDENTIFIER}name hostname\n"
            f"ssh {FIXTURE_IDENTIFIER}-2 hostname\n",
        )
        result = run_validator(self.tree.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
