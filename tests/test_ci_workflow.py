#!/usr/bin/env python3
"""The GitHub Actions workflow contract for Review-Switch."""

import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


class CiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_ci_runs_for_pull_requests(self):
        self.assertIn("pull_request:", self.workflow)

    def test_ci_matrix_covers_supported_python_versions(self):
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f'"{version}"', self.workflow)
        self.assertIn("python-version", self.workflow)
        self.assertIn("${{ matrix.python-version }}", self.workflow)

    def test_ci_runs_the_test_suite_and_residue_lint(self):
        self.assertIn("python -m pytest", self.workflow)
        self.assertIn("bash hook/tests/test-review-adjudicator.sh", self.workflow)
        self.assertIn(
            "bash skills/review-switch/tests/test-resolve-machine-config.sh",
            self.workflow,
        )
        self.assertIn("python scripts/validate_plugin_tree.py --root .", self.workflow)

    def test_ci_installs_the_bridge_runtime_dependency(self):
        self.assertIn("aiohttp", self.workflow)

    def test_ci_grants_only_read_access_to_repository_contents(self):
        self.assertIn("permissions:", self.workflow)
        self.assertIn("contents: read", self.workflow)

    def test_ci_cancels_superseded_pull_request_runs(self):
        self.assertIn("concurrency:", self.workflow)
        self.assertIn("cancel-in-progress: true", self.workflow)


if __name__ == "__main__":
    unittest.main()
