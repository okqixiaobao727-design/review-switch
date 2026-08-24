#!/usr/bin/env python3
"""Preparing a review: the fixed point, the spec, the graph, and the Axis Brief."""

import argparse
import json
import os
import pathlib
import subprocess
import unittest
from unittest import mock

from bridge_harness import FakePaneTestCase, base_args, load_bridge


class AxisBriefTests(unittest.TestCase):
    """The fixed text each axis is briefed with, and the navigation block in it."""

    def setUp(self):
        self.bridge = load_bridge()

    def test_code_graph_result_is_trimmed_to_the_navigation_contract(self):
        graph_result = {
            "risk_score": 0.93,
            "test_gaps": [{"name": "late_change"}],
            "context_savings": {"estimated_tokens_saved": 1200},
            "changed_functions": [
                {
                    "file_path": "/workspace/project/src/later.py",
                    "line_start": 30,
                    "line_end": 35,
                    "name": "late_change",
                    "risk_score": 0.12,
                },
                {
                    "file_path": "/workspace/project/src/first.py",
                    "line_start": 5,
                    "line_end": 9,
                    "name": "read_first",
                    "risk_score": 0.93,
                },
            ],
            "review_priorities": [
                {
                    "file_path": "/workspace/project/src/first.py",
                    "line_start": 5,
                    "line_end": 9,
                    "name": "read_first",
                    "risk_score": 0.93,
                }
            ],
        }

        block = self.bridge.build_navigation_block(
            graph_result, "/workspace/project"
        )

        self.assertEqual(
            block,
            "src/first.py:5–9  read_first\n"
            "src/later.py:30–35  late_change",
        )
        for excluded in ("risk_score", "test_gaps", "context_savings"):
            self.assertNotIn(excluded, block)

    def test_navigation_keeps_every_changed_function_without_a_cap(self):
        changed_functions = [
            {
                "file_path": f"/workspace/project/src/change_{index}.py",
                "line_start": index + 1,
                "line_end": index + 1,
                "name": f"change_{index}",
            }
            for index in range(12)
        ]

        block = self.bridge.build_navigation_block(
            {
                "changed_functions": changed_functions,
                "review_priorities": [],
            },
            "/workspace/project",
        )

        self.assertEqual(len(block.splitlines()), len(changed_functions))
        self.assertEqual(
            block.splitlines()[-1],
            "src/change_11.py:12–12  change_11",
        )

    def test_standards_axis_brief_matches_the_fixed_text(self):
        brief = self.bridge.build_standards_brief(
            "base-ref",
            "abc1234 feature one\ndef5678 feature two",
            ["AGENTS.md", "docs/agents/domain.md"],
        )

        self.assertEqual(
            brief,
            """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff base-ref...HEAD
Commits:
abc1234 feature one
def5678 feature two

Standards sources: AGENTS.md, docs/agents/domain.md

Smell baseline (applies even when the repo documents nothing; the repo overrides; every smell is a judgement call; skip anything tooling enforces):
- Mysterious Name: a function, variable, or type whose name doesn't reveal what it does or holds. → rename it; if no honest name comes, the design's murky.
- Duplicated Code: the same logic shape appears in more than one hunk or file in the change. → extract the shared shape, call it from both.
- Feature Envy: a method that reaches into another object's data more than its own. → move the method onto the data it envies.
- Data Clumps: the same few fields or params keep travelling together (a type wanting to be born). → bundle them into one type, pass that.
- Primitive Obsession: a primitive or string standing in for a domain concept that deserves its own type. → give the concept its own small type.
- Repeated Switches: the same switch/if-cascade on the same type recurs across the change. → replace with polymorphism, or one map both sites share.
- Shotgun Surgery: one logical change forces scattered edits across many files in the diff. → gather what changes together into one module.
- Divergent Change: one file or module is edited for several unrelated reasons. → split so each module changes for one reason.
- Speculative Generality: abstraction, parameters, or hooks added for needs the spec doesn't have. → delete it; inline back until a real need shows.
- Message Chains: long a.b().c().d() navigation the caller shouldn't depend on. → hide the walk behind one method on the first object.
- Middle Man: a class or function that mostly just delegates onward. → cut it, call the real target direct.
- Refused Bequest: a subclass or implementer that ignores or overrides most of what it inherits. → drop the inheritance, use composition.

Report, per file/hunk where relevant, (a) every place the diff violates a documented standard: cite the standard (file + the rule); and (b) any baseline smell you spot: name it and quote the hunk. Distinguish hard violations from judgement calls: documented-standard breaches can be hard, but baseline smells are always judgement calls, and a documented repo standard overrides the baseline. Skip anything tooling enforces. Under 400 words.""",
        )

    def test_spec_axis_brief_matches_the_fixed_text(self):
        brief = self.bridge.build_spec_brief(
            "base-ref",
            "abc1234 feature one\ndef5678 feature two",
            "Feature title\n\nThe feature must keep the contract.",
        )

        self.assertEqual(
            brief,
            """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff base-ref...HEAD
Commits:
abc1234 feature one
def5678 feature two

Spec:
Feature title

The feature must keep the contract.

Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words.""",
        )

    def test_both_axis_briefs_receive_the_identical_navigation_block(self):
        preparation = self.bridge.ReviewPreparation(
            fixed_point="base-ref",
            resolved_fixed_point="abc123",
            commit_list="def456 feature change",
            spec_source="spec.md",
            spec_contents="Feature spec.",
            standards_files=("AGENTS.md",),
            navigation_block=(
                "src/first.py:5–9  read_first\n"
                "src/later.py:30–35  late_change"
            ),
        )
        expected_suffix = (
            "\n\nStart here (from the code graph; the diff is the full scope):\n"
            "src/first.py:5–9  read_first\n"
            "src/later.py:30–35  late_change"
        )

        self.assertTrue(preparation.brief("standards").endswith(expected_suffix))
        self.assertTrue(preparation.brief("spec").endswith(expected_suffix))

    def test_standards_brief_names_the_documented_fallback(self):
        brief = self.bridge.build_standards_brief(
            "main", "abc123 feature change", []
        )

        self.assertIn(
            "Standards sources: none documented; baseline only", brief
        )


class PreparationTests(FakePaneTestCase):
    """What a review is prepared from before any pane opens."""

    def test_the_graph_cli_runs_once_and_its_navigation_reaches_the_brief(self):
        (self.worktree / ".code-review-graph").mkdir()
        call_log = self.install_graph_stub(
            {
                "risk_score": 0.93,
                "test_gaps": [{"name": "feature"}],
                "context_savings": {"estimated_tokens_saved": 1200},
                "changed_functions": [
                    {
                        "file_path": str(self.worktree / "feature.py"),
                        "line_start": 1,
                        "line_end": 1,
                        "name": "feature",
                        "risk_score": 0.93,
                    }
                ],
                "review_priorities": [],
            }
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        for call in calls:
            self.assertEqual(
                pathlib.Path(
                    call["argv"][call["argv"].index("--repo") + 1]
                ).resolve(),
                self.worktree.resolve(),
            )
            self.assertEqual(
                pathlib.Path(call["cwd"]).resolve(), self.worktree.resolve()
            )
        for call in calls[1:]:
            self.assertEqual(
                call["argv"][call["argv"].index("--base") + 1],
                self.fixed_point,
            )
        self.assertIn("--json", calls[0]["argv"])
        self.assertIn("--brief", calls[1]["argv"])
        self.assertNotIn("--brief", calls[2]["argv"])
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn(
            "Start here (from the code graph; the diff is the full scope):\n"
            "feature.py:1–1  feature",
            prompt,
        )
        for excluded in ("risk_score", "test_gaps", "context_savings"):
            self.assertNotIn(excluded, prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_an_empty_graph_result_contributes_no_navigation(self):
        (self.worktree / ".code-review-graph").mkdir()
        self.install_graph_stub(
            {"changed_functions": [], "review_priorities": []}
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

    def test_the_cli_resolves_a_graph_outside_the_main_checkout(self):
        external_graph = self.root / "external-graph"
        external_graph.mkdir()
        self.enter(
            mock.patch.dict(
                os.environ,
                {"CRG_DATA_DIR": str(external_graph)},
                clear=False,
            )
        )
        call_log = self.install_graph_stub(
            {
                "changed_functions": [
                    {
                        "file_path": str(self.worktree / "feature.py"),
                        "line_start": 1,
                        "line_end": 1,
                        "name": "feature",
                    }
                ],
                "review_priorities": [],
            }
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        for call in calls:
            self.assertEqual(call["dataDirEnv"], str(external_graph))
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn("feature.py:1–1  feature", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_a_main_checkout_with_no_graph_is_treated_as_unavailable(self):
        call_log = self.install_graph_stub(
            {"changed_functions": [], "review_priorities": []},
            graph_status={
                "nodes": 0,
                "files": 0,
                "last_updated": None,
                "built_on_branch": None,
                "built_at_commit": None,
            },
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual([call["argv"][0] for call in calls], ["status"])
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

    def test_an_unreadable_graph_status_contributes_no_navigation(self):
        call_log = self.install_graph_stub(
            {"changed_functions": [], "review_priorities": []},
            graph_status=[],
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual([call["argv"][0] for call in calls], ["status"])
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

    def test_an_absent_graph_cli_leaves_the_brief_unchanged(self):
        (self.worktree / ".code-review-graph").mkdir()
        self.use_graphless_path()
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertFalse(output["preparation"]["codeGraphUsed"])

    def test_a_dirty_worktree_never_queries_or_mentions_the_graph(self):
        (self.worktree / ".code-review-graph").mkdir()
        call_log = self.install_graph_stub(
            {"changed_functions": [], "review_priorities": []}
        )
        (self.worktree / "pending.txt").write_text(
            "uncommitted\n", encoding="utf-8"
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertFalse(call_log.exists())
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertFalse(output["preparation"]["codeGraphUsed"])

    def test_a_linked_worktree_queries_the_graph_at_the_main_checkout(self):
        main_checkout = self.worktree
        linked_worktree = self.root / "linked-worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(main_checkout),
                "worktree",
                "add",
                "--quiet",
                "-b",
                "feature-graph",
                str(linked_worktree),
                "HEAD",
            ],
            check=True,
        )
        (main_checkout / ".code-review-graph").mkdir()
        self.worktree = linked_worktree
        self.worktree_root = str(linked_worktree)
        call_log = self.install_graph_stub(
            {
                "changed_functions": [
                    {
                        "file_path": str(main_checkout / "feature.py"),
                        "line_start": 1,
                        "line_end": 1,
                        "name": "feature",
                    }
                ],
                "review_priorities": [],
            }
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        expected_base = f"{self.fixed_point}...feature-graph"
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        for call in calls:
            self.assertEqual(
                pathlib.Path(call["cwd"]).resolve(), main_checkout.resolve()
            )
            self.assertEqual(
                pathlib.Path(
                    call["argv"][call["argv"].index("--repo") + 1]
                ).resolve(),
                main_checkout.resolve(),
            )
        for call in calls[1:]:
            self.assertEqual(
                call["argv"][call["argv"].index("--base") + 1],
                expected_base,
            )
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn("feature.py:1–1  feature", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_an_unresolvable_fixed_point_fails_before_a_pane_opens(self):
        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "fixed point did not resolve: missing-ref"
            ):
                self.run_bridge(self.args(base="missing-ref"))

        self.assertEqual(self.codex.panes, [])

    def test_an_empty_three_dot_diff_fails_before_a_pane_opens(self):
        fixed_point = subprocess.run(
            ["git", "-C", str(self.worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(RuntimeError, "three-dot diff is empty"):
                self.run_bridge(self.args(base=fixed_point))

        self.assertEqual(self.codex.panes, [])

    def test_a_symbolic_fixed_point_is_resolved_only_for_the_report(self):
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(base="HEAD~1", spec="spec.md", axis="standards")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["fixedPoint"], self.fixed_point)
        self.assertIn("Diff: git diff HEAD~1...HEAD", prompt)

    def test_the_spec_axis_without_a_spec_fails_before_a_pane_opens(self):
        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "run with --axis standards when no spec exists"
            ):
                self.run_bridge(self.args(spec=None, axis="spec"))

        self.assertEqual(self.codex.panes, [])

    def test_both_axes_without_a_spec_fail_before_a_pane_opens(self):
        with self.assertRaisesRegex(
            RuntimeError, "run with --axis standards when no spec exists"
        ):
            self.run_bridge(self.args(spec=None, axis="both"))

        self.assertEqual(self.codex.launched_panes, [])

    def test_an_unreadable_spec_fails_before_a_pane_opens(self):
        (self.worktree / "invalid-spec.md").write_bytes(b"\xff")

        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(RuntimeError, "spec file could not be read"):
                self.run_bridge(self.args(spec="invalid-spec.md", axis="spec"))

        self.assertEqual(self.codex.panes, [])

    def test_a_missing_spec_path_is_not_reported_as_an_issue(self):
        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(RuntimeError, "spec file not found"):
                self.run_bridge(self.args(spec="docs/missing.md", axis="spec"))

        self.assertEqual(self.codex.panes, [])

    def test_a_spec_run_sends_only_the_spec_axis_brief_to_one_pane(self):
        fixed_point = self.fixed_point
        self.codex.finish("no findings")

        code, _output = self.run_bridge(
            self.args(base=fixed_point, spec="spec.md", axis="spec")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"].split("\n", 1)[1]
        self.assertEqual(code, 0)
        self.assertEqual(self.codex.panes, [])
        self.assertIn("Spec:\nFeature spec.\n\nReport:", prompt)
        self.assertNotIn("Smell baseline", prompt)
        for excluded in (
            "Rounds contract",
            "one re-review",
            "$code-review",
            "/code-review",
            "mattpocock-skills",
            "Start here (from the code graph",
        ):
            self.assertNotIn(excluded, prompt)

    def test_an_issue_spec_reaches_the_spec_axis_with_its_comments(self):
        real_run = subprocess.run

        def run(command, **kwargs):
            if command[:3] == ["gh", "issue", "view"]:
                self.assertEqual(command, ["gh", "issue", "view", "17", "--comments"])
                return argparse.Namespace(
                    returncode=0,
                    stdout="Issue title\n\nIssue body\n\nFirst comment\n",
                    stderr="",
                )
            return real_run(command, **kwargs)

        self.codex.finish("no findings")
        with mock.patch.object(self.bridge.subprocess, "run", side_effect=run):
            code, output = self.run_bridge(
                self.args(spec="17", axis="spec")
            )

        prompt = self.codex.started_turns[0]["input"][0]["text"].split("\n", 1)[1]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specSource"], "17")
        self.assertIn(
            "Spec:\nIssue title\n\nIssue body\n\nFirst comment\n\nReport:",
            prompt,
        )


if __name__ == "__main__":
    unittest.main()
