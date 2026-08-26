#!/usr/bin/env python3
"""Preparing a review: the Review Scope, the spec, the graph, and the Axis Brief."""

import json
import os
import pathlib
import subprocess
import unittest
from unittest import mock

from bridge_harness import (
    FakePaneTestCase,
    base_args,
    graph_navigation_result,
    load_bridge,
)

# Every `gh` double below is a response captured from real `gh` 2.88.1 on
# 2026-08-26, so that no test here can pass against output `gh` never
# produces (#30). The last is the same issue read the way the defect read
# it: rc=0, the comment thread, and no body at all.
#   gh issue view 23 --json number,title,body,comments
#   gh issue view 3 --json number,title,body,comments
#   gh issue view 14020 --repo cli/cli --json number,title,body,comments
#   gh issue view 11230 --repo cli/cli --json number,title,body,comments
#   gh issue view 23 --comments
GH_ISSUE_WITH_A_COMMENT = '{"body":"## Parent\\n\\nSpec: #11\\n\\n## What to build\\n\\nA caller learns from every result what it is permitted to do next, and cannot exceed the cap even if it tries. This is the only channel that reaches a Codex child, which has no skill of ours to read.\\n\\n## Acceptance criteria\\n\\n- [ ] Every result names the next permitted action: fix and stop, fix then one re-review, or escalate.\\n- [ ] A standards axis is refused any resume. A spec axis is granted exactly one, and a second is refused. A refusal reports escalate.\\n- [ ] The cap is per lineage: starting a fresh review is always available and is unaffected by a lineage that reached its cap.\\n- [ ] The result states the permitted action only and never states what escalation is — that stays the caller\'s.\\n- [ ] The cap is asserted in exactly one test file.\\n\\n## Blocked by\\n\\n- #22\\n\\n## Routing\\n\\nWorkflow: tdd\\nExecutor: claude\\nModel: claude-opus-5\\nEffort: medium\\nReview: codex gpt-5.6-luna max\\nReasons: Enforcing the cap and reporting the next permitted action are new behaviour -> tdd; next is the contract callers code against -> core; it is a per-lineage per-axis round state machine -> complex; review overridden by the user at confirmation.\\n","comments":[{"id":"IC_kwDOT4SkBc8AAAABQTzpew","author":{"login":"okqixiaobao727-design"},"authorAssociation":"OWNER","body":"/crew crewtask/2","createdAt":"2026-08-24T00:49:01Z","includesCreatedEdit":false,"isMinimized":false,"minimizedReason":"","reactionGroups":[],"url":"https://github.com/okqixiaobao727-design/review-switch/issues/23#issuecomment-5389478267","viewerDidAuthor":true}],"number":23,"title":"The Rounds Contract is enforced by the Bridge and reported as the next permitted action"}'
GH_ISSUE_WITHOUT_COMMENTS = '{"body":"## Summary\\n\\nState files written per review are only deleted when the turn **failed**; successful reviews leave their files behind forever. Measured 2026-08-18: `~/.claude/state/code-review-tui/` held **292 files (600 KB)** — now 294 — and `~/.claude/state/code-review-claude/` held **38 files (380 KB, largest 22 KB)**. Small in bytes, but the whole directory is globbed on every review launch, so the scan cost grows without bound.\\n\\nHand-off of the review-state part of agentcrew-dev-skills#83\'s residue inventory (item 6); the code paths are this repo\'s.\\n\\n## Evidence\\n\\n- `tui_review_bridge.py:984-986` — state file deleted **only when the turn failed**; the success path never unlinks.\\n- `tui_review_bridge.py:276` — the entire state directory is globbed on every review launch.\\n- `claude_review_bridge.py:237-249` — log opened with `open(\\"a\\")` per round, never rotated or deleted.\\n\\n## Possible directions\\n\\n1. Delete the state file on the success path too (mirror of the failure path).\\n2. Age-based sweep at launch: while globbing the directory anyway, unlink files older than a named retention window.\\n3. Rotate or cap the append-only claude-lane logs.","comments":[],"number":3,"title":"Review state files are never reaped on success: ~/.claude/state/code-review-tui holds 294 files and the whole dir is globbed on every launch"}'
GH_ISSUE_WITH_ONLY_A_COMMENT = '{"body":"","comments":[{"id":"IC_kwDODKw3uc8AAAABMgBcTA","author":{"login":"github-actions"},"authorAssociation":"CONTRIBUTOR","body":"This issue may have been opened accidentally. I\'m going to close it now, but feel free to open a new issue with a more descriptive title.","createdAt":"2026-07-30T16:59:10Z","includesCreatedEdit":false,"isMinimized":false,"minimizedReason":"","reactionGroups":[],"url":"https://github.com/cli/cli/issues/14020#issuecomment-5133851724","viewerDidAuthor":false},{"id":"IC_kwDODKw3uc8AAAABMgBeew","author":{"login":"github-actions"},"authorAssociation":"CONTRIBUTOR","body":"Thank you for taking the time to create this issue.\\n\\nWe\'ve automatically reviewed this issue and suspect it as potentially inauthentic or spam-like content. As a result, we\'re closing this issue.\\n\\n**If this was closed by mistake**, please don\'t hesitate to reach out to us by commenting on this issue with additional context.\\n\\nWe appreciate your understanding and apologize if this action was taken in error. Our automated systems help us manage the large volume of issues we receive, but we know they\'re not perfect.\\n","createdAt":"2026-07-30T16:59:14Z","includesCreatedEdit":false,"isMinimized":false,"minimizedReason":"","reactionGroups":[],"url":"https://github.com/cli/cli/issues/14020#issuecomment-5133852283","viewerDidAuthor":false}],"number":14020,"title":"x"}'
GH_ISSUE_WITHOUT_A_BODY_OR_COMMENT = '{"body":"","comments":[],"number":11230,"title":"Brew install"}'
GH_ISSUE_THE_COMMENTS_FLAG_WAY = 'author:\tokqixiaobao727-design\nassociation:\towner\nedited:\tfalse\nstatus:\tnone\n--\n/crew crewtask/2\n--\n'

# What `code-review-graph status --json` really answers in a checkout that has
# never been built, captured from real code-review-graph on 2026-08-26 in a
# throwaway repository. It is the reply that now sends the Bridge to `build`,
# so no test here may pass against a shape the tool never produces (#30).
#   code-review-graph status --json --repo <fresh checkout>
GRAPH_STATUS_NEVER_BUILT = {
    "nodes": 0,
    "edges": 0,
    "files": 0,
    "languages": [],
    "last_updated": None,
    "vcs": "git",
    "built_on_branch": None,
    "built_at_commit": None,
    "current_branch": "main",
    "current_sha": "fb2fa8fda4ff0ff2f3148bf366480a8dbb1666f0",
    "svn_branch": None,
    "svn_revision": None,
}


class AxisBriefTests(unittest.TestCase):
    """The fixed text each axis is briefed with, and the navigation block in it."""

    def setUp(self):
        self.bridge = load_bridge()

    def scope_at(self, fork_point):
        return self.bridge.ReviewScope(
            fixed_point="base-ref",
            resolved_fixed_point="abc123",
            fork_point=fork_point,
        )

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

        self.assertEqual(block, "src/first.py:5–9  read_first")
        self.assertNotIn("late_change", block)
        for excluded in ("risk_score", "test_gaps", "context_savings"):
            self.assertNotIn(excluded, block)

    def test_navigation_keeps_every_review_priority_without_a_cap(self):
        review_priorities = [
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
                "changed_functions": list(review_priorities),
                "review_priorities": review_priorities,
            },
            "/workspace/project",
        )

        self.assertEqual(len(block.splitlines()), len(review_priorities))
        self.assertEqual(
            block.splitlines()[-1],
            "src/change_11.py:12–12  change_11",
        )

    def test_a_scope_with_no_ranked_priorities_contributes_no_navigation(self):
        """No priorities is the absent-tool case, not a heading over nothing."""
        block = self.bridge.build_navigation_block(
            {
                "changed_functions": [
                    {
                        "file_path": f"/workspace/project/src/change_{index}.py",
                        "line_start": index + 1,
                        "line_end": index + 1,
                        "name": f"change_{index}",
                    }
                    for index in range(12)
                ],
                "review_priorities": [],
            },
            "/workspace/project",
        )

        self.assertIsNone(block)

    def test_standards_axis_brief_matches_the_fixed_text(self):
        brief = self.bridge.build_standards_brief(
            self.scope_at("0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"),
            "abc1234 feature one\ndef5678 feature two",
            ["AGENTS.md", "docs/agents/domain.md"],
        )

        self.assertEqual(
            brief,
            """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff 0f1e2d3c4b5a69788796a5b4c3d2e1f009182736
New files not in that diff: git ls-files --others --exclude-standard
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
            self.scope_at("0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"),
            "abc1234 feature one\ndef5678 feature two",
            "Feature title\n\nThe feature must keep the contract.",
        )

        self.assertEqual(
            brief,
            """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff 0f1e2d3c4b5a69788796a5b4c3d2e1f009182736
New files not in that diff: git ls-files --others --exclude-standard
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
            scope=self.bridge.ReviewScope(
                fixed_point="base-ref",
                resolved_fixed_point="abc123",
                fork_point="fed321",
            ),
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
            "\n\nStart here (from the code graph; the two commands above are the full scope):\n"
            "src/first.py:5–9  read_first\n"
            "src/later.py:30–35  late_change"
        )

        self.assertTrue(
            preparation.brief("standards").text.endswith(expected_suffix)
        )
        self.assertTrue(preparation.brief("spec").text.endswith(expected_suffix))

    def test_preparation_yields_one_axis_brief_per_requested_axis(self):
        args = base_args(axis="both")

        briefs = self.bridge.axis_briefs(args)

        self.assertEqual([brief.axis for brief in briefs], ["standards", "spec"])
        self.assertEqual(
            [brief.text for brief in briefs],
            ["Read-only standards review", "Read-only spec review"],
        )

    def test_one_requested_axis_yields_that_axis_brief_alone(self):
        args = base_args(axis="spec")

        briefs = self.bridge.axis_briefs(args)

        self.assertEqual([brief.axis for brief in briefs], ["spec"])

    def test_standards_brief_names_the_documented_fallback(self):
        brief = self.bridge.build_standards_brief(
            self.scope_at("abc123"), "abc123 feature change", []
        )

        self.assertIn(
            "Standards sources: none documented; baseline only", brief
        )


class PreparationTests(FakePaneTestCase):
    """What a review is prepared from before any pane opens."""

    def fixed_point_that_has_moved_on(self):
        """A branch at the fork point that then commits on without this tree.

        The shape a stale fixed point has in life: the branch names a commit
        the working tree forked away from, so the branch and
        `git merge-base <branch> HEAD` are two different revisions. Its name is
        handed back for the review to be based on.
        """
        def git(*arguments):
            result = subprocess.run(
                ["git", "-C", str(self.worktree), *arguments],
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()

        moved = git(
            "commit-tree",
            f"{self.fixed_point}^{{tree}}",
            "-p",
            self.fixed_point,
            "-m",
            "the fixed point moves on",
        )
        self.assertNotEqual(moved, self.fixed_point)
        git("update-ref", "refs/heads/trunk", moved)
        return "trunk"

    def test_the_graph_cli_runs_once_and_its_navigation_reaches_the_brief(self):
        (self.worktree / ".code-review-graph").mkdir()
        feature = {
            "file_path": str(self.worktree / "feature.py"),
            "line_start": 1,
            "line_end": 1,
            "name": "feature",
            "risk_score": 0.93,
        }
        unranked_test = {
            "file_path": str(self.worktree / "tests/test_feature.py"),
            "line_start": 1,
            "line_end": 1,
            "name": "test_feature",
            "risk_score": 0.04,
        }
        call_log = self.install_graph_stub(
            graph_navigation_result(
                feature,
                changed_functions=[feature, unranked_test],
                risk_score=0.93,
                test_gaps=[{"name": "feature"}],
                context_savings={"estimated_tokens_saved": 1200},
            )
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = self.graph_calls(call_log)
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
            "Start here (from the code graph; the two commands above are the full scope):\n"
            "feature.py:1–1  feature",
            prompt,
        )
        self.assertNotIn("test_feature", prompt)
        for excluded in ("risk_score", "test_gaps", "context_savings"):
            self.assertNotIn(excluded, prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_an_empty_graph_result_contributes_no_navigation(self):
        (self.worktree / ".code-review-graph").mkdir()
        self.install_graph_stub(graph_navigation_result())
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

    def test_an_operator_data_dir_never_reaches_the_graph_cli(self):
        """One data directory holds one graph, so honouring it crosses checkouts.

        Measured against real code-review-graph: two repositories built with
        one `CRG_DATA_DIR` leave a single `graph.db`, the second build evicting
        the first, and `status` then answers both with the same
        `built_at_commit`. A checkout owns its graph (ADR-0005), so the Bridge
        takes the variable out rather than letting a review navigate by the
        previous review's map.
        """
        external_graph = self.root / "external-graph"
        external_graph.mkdir()
        self.enter(
            mock.patch.dict(
                os.environ,
                {"CRG_DATA_DIR": str(external_graph)},
                clear=False,
            )
        )
        feature = {
            "file_path": str(self.worktree / "feature.py"),
            "line_start": 1,
            "line_end": 1,
            "name": "feature",
        }
        call_log = self.install_graph_stub(graph_navigation_result(feature))
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = self.graph_calls(call_log)
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        for call in calls:
            self.assertIsNone(call["dataDirEnv"])
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn("feature.py:1–1  feature", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_a_main_checkout_with_no_graph_is_built_rather_than_skipped(self):
        feature = {
            "file_path": str(self.worktree / "feature.py"),
            "line_start": 1,
            "line_end": 1,
            "name": "feature",
        }
        call_log = self.install_graph_stub(
            graph_navigation_result(feature),
            graph_status=GRAPH_STATUS_NEVER_BUILT,
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = self.graph_calls(call_log)
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "build", "detect-changes"],
        )
        self.assertIn("feature.py:1–1  feature", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_an_unreadable_graph_status_contributes_no_navigation(self):
        call_log = self.install_graph_stub(
            graph_navigation_result(),
            graph_status=[],
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = self.graph_calls(call_log)
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

    def test_a_dirty_main_checkout_still_gets_its_navigation_block(self):
        """`update --brief` re-parses changed files from disk, dirty or not."""
        (self.worktree / ".code-review-graph").mkdir()
        pending = {
            "file_path": str(self.worktree / "pending.py"),
            "line_start": 1,
            "line_end": 4,
            "name": "pending",
        }
        call_log = self.install_graph_stub(graph_navigation_result(pending))
        (self.worktree / "pending.py").write_text(
            "def pending():\n    return True\n", encoding="utf-8"
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = self.graph_calls(call_log)
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        self.assertIn("pending.py:1–4  pending", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_the_graph_is_based_on_the_fork_point_the_diff_uses(self):
        """A fixed point that has moved on names a range the diff never reads."""
        (self.worktree / ".code-review-graph").mkdir()
        moved_fixed_point = self.fixed_point_that_has_moved_on()
        feature = {
            "file_path": str(self.worktree / "feature.py"),
            "line_start": 1,
            "line_end": 1,
            "name": "feature",
        }
        call_log = self.install_graph_stub(graph_navigation_result(feature))
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(base=moved_fixed_point, axis="spec")
        )

        calls = self.graph_calls(call_log)
        self.assertEqual(code, 0)
        for call in calls[1:]:
            self.assertEqual(
                call["argv"][call["argv"].index("--base") + 1],
                self.fixed_point,
            )
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn(f"Diff: git diff {self.fixed_point}", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_a_worktree_with_no_graph_is_built_where_it_stands(self):
        """The worktree owns its graph, so it is built there, not consulted elsewhere."""
        main_checkout = self.use_linked_worktree()
        (main_checkout / ".code-review-graph").mkdir()
        feature = {
            "file_path": str(self.worktree / "feature.py"),
            "line_start": 1,
            "line_end": 1,
            "name": "feature",
        }
        call_log = self.install_graph_stub(
            graph_navigation_result(feature),
            graph_status=GRAPH_STATUS_NEVER_BUILT,
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = self.graph_calls(call_log)
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "build", "detect-changes"],
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
        self.assertNotIn("--base", calls[1]["argv"])
        self.assertEqual(
            calls[2]["argv"][calls[2]["argv"].index("--base") + 1],
            self.fixed_point,
        )
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn(
            "Start here (from the code graph; the two commands above are the full scope):\n"
            "feature.py:1–1  feature",
            prompt,
        )
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_a_worktree_with_a_graph_is_updated_rather_than_built(self):
        self.use_linked_worktree()
        feature = {
            "file_path": str(self.worktree / "feature.py"),
            "line_start": 1,
            "line_end": 1,
            "name": "feature",
        }
        call_log = self.install_graph_stub(graph_navigation_result(feature))
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = self.graph_calls(call_log)
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        self.assertIn("--brief", calls[1]["argv"])
        for call in calls[1:]:
            self.assertEqual(
                call["argv"][call["argv"].index("--base") + 1],
                self.fixed_point,
            )
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn("feature.py:1–1  feature", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_a_failing_build_leaves_the_brief_unchanged(self):
        self.use_linked_worktree()
        call_log = self.install_graph_stub(
            graph_navigation_result(),
            graph_status=GRAPH_STATUS_NEVER_BUILT,
            build_returncode=1,
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = self.graph_calls(call_log)
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls], ["status", "build"]
        )
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

    def test_a_build_past_its_bound_leaves_the_brief_unchanged(self):
        """The one unbounded call in the flow, bounded; overrunning it is absence."""
        self.use_linked_worktree()
        call_log = self.install_graph_stub(
            graph_navigation_result(),
            graph_status=GRAPH_STATUS_NEVER_BUILT,
            build_seconds=30,
        )
        self.enter(
            mock.patch.object(
                self.bridge, "CODE_GRAPH_BUILD_TIMEOUT_SECONDS", 0.5
            )
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = self.graph_calls(call_log)
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls], ["status", "build"]
        )
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

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

    def test_a_symbolic_fixed_point_is_resolved_only_for_the_report(self):
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(base="HEAD~1", spec="spec.md", axis="standards")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["fixedPoint"], self.fixed_point)
        self.assertIn(f"Diff: git diff {self.fixed_point}", prompt)

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

    def test_an_unreadable_spec_degrades_instead_of_stopping_the_review(self):
        (self.worktree / "invalid-spec.md").write_bytes(b"\xff")
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(spec="invalid-spec.md", axis="spec")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["specSource"], "not fetched: invalid-spec.md"
        )
        self.assertIn("Reference as given: invalid-spec.md", prompt)
        self.assertIn("spec file could not be read", prompt)

    def test_a_missing_spec_path_degrades_and_is_never_sent_to_github(self):
        calls = self.fake_gh(stdout=GH_ISSUE_WITHOUT_COMMENTS)
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(spec="docs/missing.md", axis="spec")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(
            output["preparation"]["specSource"], "not fetched: docs/missing.md"
        )
        self.assertIn("Reference as given: docs/missing.md", prompt)
        self.assertIn("Failure: spec file not found", prompt)

    def test_an_empty_spec_file_counts_as_not_fetched(self):
        (self.worktree / "blank-spec.md").write_text("   \n\n", encoding="utf-8")
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(spec="blank-spec.md", axis="spec")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["specSource"], "not fetched: blank-spec.md"
        )
        self.assertIn("spec file is empty", prompt)

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

    def test_an_issue_spec_reaches_the_spec_axis_with_its_body_and_comments(self):
        self.fake_gh(
            stdout=GH_ISSUE_WITH_A_COMMENT,
            plain_stdout=GH_ISSUE_THE_COMMENTS_FLAG_WAY,
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="23", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specSource"], "23")
        self.assertIn(
            "#23 The Rounds Contract is enforced by the Bridge and reported as "
            "the next permitted action",
            prompt,
        )
        self.assertIn("A caller learns from every result", prompt)
        self.assertIn(
            "Comment from okqixiaobao727-design:\n/crew crewtask/2", prompt
        )

    def test_an_issue_without_comments_reaches_the_spec_axis_with_its_body(self):
        self.fake_gh(stdout=GH_ISSUE_WITHOUT_COMMENTS)
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="#3", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specSource"], "#3")
        self.assertIn(
            "#3 Review state files are never reaped on success", prompt
        )
        self.assertIn("State files written per review", prompt)
        self.assertNotIn("Comment from", prompt)

    def test_an_issue_whose_requirements_are_only_in_a_comment_reaches_the_brief(self):
        self.fake_gh(stdout=GH_ISSUE_WITH_ONLY_A_COMMENT)
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="14020", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specSource"], "14020")
        self.assertIn(
            "Comment from github-actions:\nThis issue may have been opened "
            "accidentally.",
            prompt,
        )
        self.assertNotIn("could not be fetched", prompt)

    def test_an_issue_url_is_fetched_rather_than_read_as_a_file(self):
        calls = self.fake_gh(stdout=GH_ISSUE_WITH_A_COMMENT)
        reference = (
            "https://github.com/okqixiaobao727-design/review-switch/issues/23"
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec=reference, axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["preparation"]["specSource"], reference)
        self.assertIn("A caller learns from every result", prompt)

    def test_a_gh_failure_degrades_the_spec_axis_instead_of_stopping_it(self):
        self.fake_gh(
            returncode=1,
            stderr=(
                "GraphQL: Could not resolve to an issue or pull request with "
                "the number of 99999. (repository.issue)\n"
            ),
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="99999", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["specSource"], "not fetched: 99999"
        )
        self.assertIn("Reference as given: 99999", prompt)
        self.assertIn(
            "Could not resolve to an issue or pull request with the number of "
            "99999",
            prompt,
        )
        self.assertIn("Do not infer", prompt)

    def test_an_issue_with_neither_body_nor_comment_counts_as_not_fetched(self):
        self.fake_gh(stdout=GH_ISSUE_WITHOUT_A_BODY_OR_COMMENT)
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="11230", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["specSource"], "not fetched: 11230"
        )
        self.assertIn("the issue has no body and no comments", prompt)


class ReviewScopeTests(FakePaneTestCase):
    """The fixed point to the working tree as it stands, committed or not."""

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.worktree), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def tree_and_index_state(self):
        return (
            self.git("status", "--porcelain"),
            self.git("ls-files", "--stage"),
            sorted(
                (
                    path.relative_to(self.worktree).as_posix(),
                    path.read_bytes(),
                )
                for path in self.worktree.rglob("*")
                if path.is_file() and ".git/" not in path.as_posix()
            ),
        )

    def scope_commands(self, prompt):
        """The two lines the brief prints, as the reviewer would run them."""
        diff_line = next(
            line for line in prompt.splitlines() if line.startswith("Diff: ")
        )
        untracked_line = next(
            line
            for line in prompt.splitlines()
            if line.startswith("New files not in that diff: ")
        )
        return (
            diff_line.split(": ", 1)[1].split(),
            untracked_line.split(": ", 1)[1].split(),
        )

    def write_work_of_every_kind(self):
        """One committed, one staged, one unstaged, and one untracked change."""
        (self.worktree / "staged.py").write_text(
            "STAGED = True\n", encoding="utf-8"
        )
        self.git("add", "staged.py")
        (self.worktree / "README.md").write_text(
            "baseline\nunstaged\n", encoding="utf-8"
        )
        (self.worktree / "untracked.py").write_text(
            "UNTRACKED = True\n", encoding="utf-8"
        )

    def test_the_scope_reaches_committed_staged_unstaged_and_untracked_work(self):
        self.write_work_of_every_kind()
        self.codex.finish("no findings")

        code, _output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        diff_command, untracked_command = self.scope_commands(prompt)
        before = self.tree_and_index_state()
        diff = self.git(*diff_command[1:])
        untracked = self.git(*untracked_command[1:])

        self.assertEqual(code, 0)
        self.assertEqual(diff_command[0], "git")
        self.assertEqual(untracked_command[0], "git")
        for path in ("feature.py", "staged.py", "README.md"):
            self.assertIn(f"b/{path}", diff)
        self.assertNotIn("b/untracked.py", diff)
        self.assertEqual(untracked.split(), ["untracked.py"])
        self.assertEqual(self.tree_and_index_state(), before)

    def test_the_diff_line_names_the_fork_point_as_a_resolved_sha(self):
        fork_point = self.head()
        self.git("checkout", "--quiet", "-b", "diverged", fork_point)
        (self.worktree / "elsewhere.py").write_text(
            "ELSEWHERE = True\n", encoding="utf-8"
        )
        self.git("add", "elsewhere.py")
        self.git("commit", "--quiet", "-m", "diverged change")
        diverged = self.head()
        self.git("checkout", "--quiet", "-")
        (self.worktree / "later.py").write_text("LATER = True\n", encoding="utf-8")
        self.git("add", "later.py")
        self.git("commit", "--quiet", "-m", "later change")
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(base="diverged", axis="standards")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        diff_command, _untracked_command = self.scope_commands(prompt)
        diff = self.git(*diff_command[1:])

        self.assertEqual(code, 0)
        self.assertEqual(diff_command, ["git", "diff", fork_point])
        self.assertEqual(output["preparation"]["fixedPoint"], diverged)
        self.assertNotIn("b/elsewhere.py", diff)
        self.assertIn("b/later.py", diff)

    def test_a_tree_matching_the_fixed_point_fails_before_a_pane_opens(self):
        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(RuntimeError, "nothing to review"):
                self.run_bridge(self.args(base=self.head()))

        self.assertEqual(self.codex.panes, [])

    def test_an_ignored_file_alone_leaves_nothing_to_review(self):
        (self.worktree / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "--quiet", "-m", "ignore rule")
        (self.worktree / "ignored.py").write_text(
            "IGNORED = True\n", encoding="utf-8"
        )

        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(RuntimeError, "nothing to review"):
                self.run_bridge(self.args(base=self.head()))

        self.assertEqual(self.codex.panes, [])

    def test_work_never_committed_runs_end_to_end_and_returns_findings(self):
        fixed_point = self.head()
        (self.worktree / "README.md").write_text(
            "baseline\nunstaged\n", encoding="utf-8"
        )
        (self.worktree / "brand-new.py").write_text(
            "BRAND_NEW = True\n", encoding="utf-8"
        )
        self.codex.finish("one finding: BRAND_NEW is unused")

        code, output = self.run_bridge(
            self.args(base=fixed_point, spec="spec.md", axis="standards")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"],
            "one finding: BRAND_NEW is unused",
        )
        self.assertIn("Commits:\n\n", prompt)
        self.assertNotIn("Start here (from the code graph", prompt)


if __name__ == "__main__":
    unittest.main()
