#!/usr/bin/env python3
"""Preparing a review: the Review Scope, the spec, the graph, and the Axis Brief."""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from bridge_harness import (
    FakePaneTestCase,
    base_args,
    initialize_review_repo,
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
            "Spec: /state/code-review-tui/2f0d-spec.md — #42 Feature title, "
            "body and 2 comments. Read it before reviewing.",
        )

        self.assertEqual(
            brief,
            """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff 0f1e2d3c4b5a69788796a5b4c3d2e1f009182736
New files not in that diff: git ls-files --others --exclude-standard
Commits:
abc1234 feature one
def5678 feature two

Spec: /state/code-review-tui/2f0d-spec.md — #42 Feature title, body and 2 comments. Read it before reviewing.

Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words.""",
        )

    def test_a_spec_slot_names_the_file_and_what_the_file_holds(self):
        self.assertEqual(
            self.bridge.build_spec_slot(
                "/state/code-review-tui/2f0d-spec.md",
                "#42 Feature title, body and 2 comments",
            ),
            "Spec: /state/code-review-tui/2f0d-spec.md — #42 Feature title, "
            "body and 2 comments. Read it before reviewing.",
        )

    def test_a_spec_slot_for_a_file_already_in_the_checkout_names_it_alone(self):
        self.assertEqual(
            self.bridge.build_spec_slot("docs/feature.md"),
            "Spec: docs/feature.md. Read it before reviewing.",
        )

    def test_an_issue_spec_says_what_it_holds_beside_where_it_is(self):
        for payload, summary in (
            (
                GH_ISSUE_WITH_A_COMMENT,
                "#23 The Rounds Contract is enforced by the Bridge and "
                "reported as the next permitted action, body and 1 comment",
            ),
            (
                GH_ISSUE_WITHOUT_COMMENTS,
                "#3 Review state files are never reaped on success: "
                "~/.claude/state/code-review-tui holds 294 files and the "
                "whole dir is globbed on every launch, body",
            ),
            (GH_ISSUE_WITH_ONLY_A_COMMENT, "#14020 x, 2 comments"),
        ):
            with self.subTest(summary=summary):
                spec = self.bridge.build_issue_spec(json.loads(payload))

                self.assertEqual(spec.summary, summary)

    def test_both_axis_briefs_receive_the_identical_navigation_block(self):
        preparation = self.bridge.ReviewPreparation(
            scope=self.bridge.ReviewScope(
                fixed_point="base-ref",
                resolved_fixed_point="abc123",
                fork_point="fed321",
            ),
            commit_list="def456 feature change",
            spec=self.bridge.SpecSlot(
                source="spec.md",
                text="Spec: spec.md. Read it before reviewing.",
                file="spec.md",
            ),
            standards=self.bridge.StandardsSources(files=("AGENTS.md",)),
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


CONVENTION_DOCUMENT = "docs/agents/issue-tracker.md"


def write_convention(root, relative=CONVENTION_DOCUMENT):
    """Put a convention document in a checkout, without tracking it."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Issues live in GitHub.\n", encoding="utf-8")
    return path


def exclude_convention_directory(root):
    """Ignore it the way the reported repository ignored it: locally (#40)."""
    info = root / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text("/docs/agents/\n", encoding="utf-8")


def write_ignore_rules(root, patterns, directory=""):
    """Ignore standards the way a repository publishing a lean tree does.

    The rule goes in a `.gitignore`, which the caller then tracks or leaves
    untracked: a tracked one is the declaration every checkout of the commit
    reads, and an untracked one is one machine's private state (ADR-0013).
    """
    path = root / directory / ".gitignore" if directory else root / ".gitignore"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{pattern}\n" for pattern in patterns), encoding="utf-8"
    )
    return path


class StandardsSourcesTests(unittest.TestCase):
    """Which documents a review is held to, and how the checkout carries them.

    Every fixture here is a real checkout: the defect (#40) is a disagreement
    between what lies on the disk and what git tracks, and a stubbed git
    cannot disagree with itself.
    """

    CONVENTION = CONVENTION_DOCUMENT

    def setUp(self):
        self.bridge = load_bridge()
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = pathlib.Path(self.work.name) / "checkout"
        self.root.mkdir()
        initialize_review_repo(self.root)

    def git(self, *arguments, cwd=None):
        return subprocess.run(
            ["git", "-C", str(cwd or self.root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def commit_all(self):
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "the convention as it ships")

    def linked_worktree(self):
        """A second checkout of the same commit, as `git worktree add` makes one."""
        linked = pathlib.Path(self.work.name) / "linked"
        self.git("worktree", "add", "--quiet", "--detach", str(linked))
        return linked

    def sources(self, root=None):
        return self.bridge.read_standards_sources(str(root or self.root))

    def test_a_tracked_convention_is_a_standards_source(self):
        write_convention(self.root)
        self.commit_all()

        sources = self.sources()

        self.assertEqual(sources.files, ("AGENTS.md", self.CONVENTION))
        self.assertEqual(sources.untracked, ())
        self.assertEqual(sources.condition, "all tracked")

    def test_a_convention_present_but_untracked_is_named_not_included(self):
        """The reported shape: on the disk, excluded, reaching no other checkout."""
        exclude_convention_directory(self.root)
        write_convention(self.root)

        sources = self.sources()

        self.assertEqual(sources.files, ("AGENTS.md",))
        self.assertEqual(sources.untracked, (self.CONVENTION,))
        self.assertEqual(
            sources.condition, f"present but untracked: {self.CONVENTION}"
        )

    def test_a_convention_that_was_never_installed_is_absent_not_all_tracked(self):
        """A checkout keeping `AGENTS.md` and no `docs/agents/` states no tracker."""
        sources = self.sources()

        self.assertEqual(sources.files, ("AGENTS.md",))
        self.assertEqual(sources.untracked, ())
        self.assertEqual(sources.condition, "absent")

    def test_absence_is_stated_beside_whatever_else_is_untracked(self):
        """One untracked file elsewhere must not swallow the convention's absence."""
        (self.root / "CONTRIBUTING.md").write_text("house style\n", encoding="utf-8")

        sources = self.sources()

        self.assertEqual(sources.files, ("AGENTS.md",))
        self.assertEqual(sources.untracked, ("CONTRIBUTING.md",))
        self.assertEqual(
            sources.condition, "absent; present but untracked: CONTRIBUTING.md"
        )

    def test_a_checkout_documenting_nothing_at_all_reports_absence(self):
        (self.root / "AGENTS.md").unlink()
        self.commit_all()

        sources = self.sources()

        self.assertEqual(sources.files, ())
        self.assertEqual(sources.untracked, ())
        self.assertEqual(sources.condition, "absent")

    def test_a_linked_worktree_resolves_what_the_main_worktree_resolves(self):
        """The defect: the same commit, two checkouts, two answers (#40)."""
        exclude_convention_directory(self.root)
        write_convention(self.root)
        tracked = "docs/agents/triage-labels.md"
        write_convention(self.root, tracked)
        self.git("add", "-f", tracked)
        self.commit_all()

        main = self.sources()
        linked = self.sources(self.linked_worktree())

        self.assertEqual(linked.files, main.files)
        self.assertEqual(linked.files, ("AGENTS.md", tracked))
        self.assertEqual(main.untracked, (self.CONVENTION,))
        self.assertEqual(linked.untracked, ())

    def untrack(self, *paths):
        """Take a path out of the index while leaving it on the disk."""
        self.git("rm", "--cached", "--quiet", *paths)

    def write_root_standards(self):
        """The root documents the reported repository keeps out of its tree."""
        (self.root / "CLAUDE.md").write_text("house rules\n", encoding="utf-8")
        write_convention(self.root)

    def test_a_tracked_gitignore_declares_its_standards_local(self):
        """The reported repository's shape: ignored by a rule every clone reads."""
        self.write_root_standards()
        write_ignore_rules(self.root, ("CLAUDE.md", "AGENTS.md", "docs/agents"))
        self.untrack("AGENTS.md")
        self.commit_all()

        sources = self.sources()

        self.assertEqual(
            sources.files, ("AGENTS.md", "CLAUDE.md", self.CONVENTION)
        )
        self.assertEqual(sources.untracked, ())
        self.assertEqual(sources.condition, "all tracked")

    def test_a_local_exclude_declares_nothing(self):
        """`.git/info/exclude` is one machine's private state, as #40 found it."""
        self.write_root_standards()
        exclude_convention_directory(self.root)
        (self.root / ".git" / "info" / "exclude").write_text(
            "/docs/agents/\nCLAUDE.md\nAGENTS.md\n", encoding="utf-8"
        )
        self.untrack("AGENTS.md")
        self.commit_all()

        sources = self.sources()

        self.assertEqual(sources.files, ())
        self.assertEqual(
            sources.untracked, ("AGENTS.md", "CLAUDE.md", self.CONVENTION)
        )
        self.assertEqual(
            sources.condition,
            "present but untracked: AGENTS.md, CLAUDE.md, " + self.CONVENTION,
        )

    def test_a_global_excludes_file_declares_nothing(self):
        """A file outside the repository reaches no clone of it."""
        self.write_root_standards()
        excludes = pathlib.Path(self.work.name) / "global-excludes"
        excludes.write_text(
            "CLAUDE.md\nAGENTS.md\ndocs/agents\n", encoding="utf-8"
        )
        self.git("config", "core.excludesFile", str(excludes))
        self.untrack("AGENTS.md")
        self.commit_all()

        sources = self.sources()

        self.assertEqual(sources.files, ())
        self.assertEqual(
            sources.untracked, ("AGENTS.md", "CLAUDE.md", self.CONVENTION)
        )

    def test_an_untracked_gitignore_declares_nothing(self):
        """A rule file no other checkout carries is no declaration."""
        self.write_root_standards()
        write_ignore_rules(
            self.root, (".gitignore", "CLAUDE.md", "AGENTS.md", "docs/agents")
        )
        self.untrack("AGENTS.md")
        self.commit_all()

        sources = self.sources()

        self.assertEqual(sources.files, ())
        self.assertEqual(
            sources.untracked, ("AGENTS.md", "CLAUDE.md", self.CONVENTION)
        )

    def test_a_rule_that_un_ignores_a_document_declares_nothing(self):
        """`check-ignore` reports a `!` rule as the match; it ignores nothing."""
        self.write_root_standards()
        write_ignore_rules(
            self.root, ("CLAUDE.md", "AGENTS.md", "!AGENTS.md", "docs/agents")
        )
        self.commit_all()
        self.untrack("AGENTS.md")
        self.git("commit", "--quiet", "-m", "stop publishing the root standard")

        sources = self.sources()

        self.assertEqual(sources.files, ("CLAUDE.md", self.CONVENTION))
        self.assertEqual(sources.untracked, ("AGENTS.md",))
        self.assertEqual(
            sources.condition, "present but untracked: AGENTS.md"
        )

    def test_a_tracked_subdirectory_gitignore_declares_its_directory_local(self):
        """The rule may live beside what it ignores, as git lets it."""
        write_convention(self.root)
        write_ignore_rules(self.root, ("agents",), directory="docs")
        self.commit_all()

        sources = self.sources()

        self.assertEqual(sources.files, ("AGENTS.md", self.CONVENTION))
        self.assertEqual(sources.untracked, ())
        self.assertEqual(sources.condition, "all tracked")

    def test_declared_local_joins_the_tracked_list_and_leaves_the_rest_named(self):
        """One tracked, one declared local, one merely lying there."""
        (self.root / "CLAUDE.md").write_text("house rules\n", encoding="utf-8")
        write_ignore_rules(self.root, ("CLAUDE.md",))
        self.commit_all()
        write_convention(self.root)

        sources = self.sources()

        self.assertEqual(sources.files, ("AGENTS.md", "CLAUDE.md"))
        self.assertEqual(sources.untracked, (self.CONVENTION,))
        self.assertEqual(
            sources.condition, f"present but untracked: {self.CONVENTION}"
        )

    def test_a_linked_worktree_reads_declared_local_documents_through_links(self):
        """How the declared files reach a worktree: the consumer's own hook.

        `git check-ignore` refuses a pathspec beyond a symbolic link, so the
        directory link is asked about as itself while the file link is asked
        about as the path it already is (#51).
        """
        self.write_root_standards()
        write_ignore_rules(self.root, ("CLAUDE.md", "AGENTS.md", "docs/agents"))
        self.untrack("AGENTS.md")
        self.commit_all()
        linked = self.linked_worktree()
        (linked / "CLAUDE.md").symlink_to(self.root / "CLAUDE.md")
        (linked / "AGENTS.md").symlink_to(self.root / "AGENTS.md")
        (linked / "docs").mkdir(exist_ok=True)
        (linked / "docs" / "agents").symlink_to(
            self.root / "docs" / "agents", target_is_directory=True
        )
        (linked / "CONTRIBUTING.md").symlink_to(self.root / "CONTRIBUTING.md")

        main = self.sources()
        worktree = self.sources(linked)

        self.assertEqual(worktree.files, main.files)
        self.assertEqual(
            worktree.files, ("AGENTS.md", "CLAUDE.md", self.CONVENTION)
        )
        self.assertEqual(worktree.untracked, ())
        self.assertEqual(worktree.condition, "all tracked")

    def test_a_tree_that_is_no_checkout_keeps_reading_the_disk(self):
        """An export has no index to ask, so the walk stays its answer (085dbed)."""
        exported = pathlib.Path(self.work.name) / "exported"
        exported.mkdir()
        (exported / "AGENTS.md").write_text("standards\n", encoding="utf-8")
        (exported / "docs" / "agents").mkdir(parents=True)
        (exported / self.CONVENTION).write_text("convention\n", encoding="utf-8")

        sources = self.sources(exported)

        self.assertEqual(sources.files, ("AGENTS.md", self.CONVENTION))
        self.assertIsNone(sources.untracked)
        self.assertEqual(
            sources.condition, "not a git checkout; read from the disk"
        )

    def test_a_non_checkout_tree_inside_a_checkout_is_read_from_the_disk(self):
        """git searches upwards, so an unpacked tree must not get its host's answer."""
        nested = self.root / "vendor" / "exported"
        nested.mkdir(parents=True)
        (nested / "AGENTS.md").write_text("standards\n", encoding="utf-8")

        sources = self.sources(nested)

        self.assertEqual(sources.files, ("AGENTS.md",))
        self.assertIsNone(sources.untracked)


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
        failure = output["preparation"]["specFailure"]
        self.assertTrue(failure.startswith("spec file could not be read: "))
        self.assertIn(f"Failure: {failure}", prompt)
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
        self.assertEqual(
            output["preparation"]["specFailure"], "spec file not found"
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
        self.assertEqual(
            output["preparation"]["specFailure"], "spec file is empty"
        )
        self.assertIn("spec file is empty", prompt)

    def spec_lines(self, prompt):
        """Every line of the brief that fills the Spec slot, however it was filled."""
        return [line for line in prompt.splitlines() if line.startswith("Spec:")]

    def untracked_files(self):
        """The new files the Review Scope holds, read with the Scope's own command.

        The Scope's command rather than a spelling of our own, because the
        point of the assertion is that preparation never adds a file the review
        would then review as work (#33).
        """
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.worktree),
                *self.bridge.ReviewScope.UNTRACKED_ARGUMENTS,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_a_spec_run_sends_only_the_spec_axis_brief_to_one_pane(self):
        fixed_point = self.fixed_point
        self.codex.finish("no findings")

        code, _output = self.run_bridge(
            self.args(base=fixed_point, spec="spec.md", axis="spec")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"].split("\n", 1)[1]
        self.assertEqual(code, 0)
        self.assertEqual(self.codex.panes, [])
        self.assertIn(
            "Spec: spec.md. Read it before reviewing.\n\nReport:", prompt
        )
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

    def test_an_issue_spec_reaches_the_spec_axis_as_the_one_file_it_names(self):
        self.fake_gh(
            stdout=GH_ISSUE_WITH_A_COMMENT,
            plain_stdout=GH_ISSUE_THE_COMMENTS_FLAG_WAY,
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="23", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        spec_file = output["preparation"]["specFile"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specSource"], "23")
        self.assertIsNone(output["preparation"]["specFailure"])
        self.assertEqual(pathlib.Path(spec_file).parent, self.state_dir)
        self.assertTrue(pathlib.Path(spec_file).is_absolute())
        self.assertEqual(
            self.spec_lines(prompt),
            [
                f"Spec: {spec_file} — #23 The Rounds Contract is enforced by "
                "the Bridge and reported as the next permitted action, body "
                "and 1 comment. Read it before reviewing."
            ],
        )
        # The whole thread is in the file the Lane is sent to, laid out as #30
        # laid it out, and no line of it is in the brief.
        contents = pathlib.Path(spec_file).read_text(encoding="utf-8")
        self.assertIn("A caller learns from every result", contents)
        self.assertIn(
            "Comment from okqixiaobao727-design:\n/crew crewtask/2", contents
        )
        self.assertNotIn("A caller learns from every result", prompt)
        self.assertNotIn("Comment from", prompt)

    def test_an_issue_without_comments_reaches_the_spec_axis_with_its_body(self):
        self.fake_gh(stdout=GH_ISSUE_WITHOUT_COMMENTS)
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="#3", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        contents = pathlib.Path(
            output["preparation"]["specFile"]
        ).read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specSource"], "#3")
        self.assertIn(
            "#3 Review state files are never reaped on success", contents
        )
        self.assertIn("State files written per review", contents)
        self.assertNotIn("Comment from", contents)
        self.assertIn(", body. Read it before reviewing.", prompt)

    def test_an_issue_whose_requirements_are_only_in_a_comment_reaches_the_brief(self):
        self.fake_gh(stdout=GH_ISSUE_WITH_ONLY_A_COMMENT)
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="14020", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        contents = pathlib.Path(
            output["preparation"]["specFile"]
        ).read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specSource"], "14020")
        self.assertIn(
            "Comment from github-actions:\nThis issue may have been opened "
            "accidentally.",
            contents,
        )
        self.assertIn("#14020 x, 2 comments. Read it before reviewing.", prompt)
        self.assertNotIn("could not be fetched", prompt)

    def test_an_issue_url_is_fetched_rather_than_read_as_a_file(self):
        calls = self.fake_gh(stdout=GH_ISSUE_WITH_A_COMMENT)
        reference = (
            "https://github.com/okqixiaobao727-design/review-switch/issues/23"
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec=reference, axis="spec"))

        contents = pathlib.Path(
            output["preparation"]["specFile"]
        ).read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output["preparation"]["specSource"], reference)
        self.assertIn("A caller learns from every result", contents)

    def test_a_spec_file_in_the_checkout_is_named_where_it_lies_and_not_copied(self):
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="spec.md", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specFile"], "spec.md")
        self.assertIsNone(output["preparation"]["specFailure"])
        self.assertEqual(
            self.spec_lines(prompt),
            ["Spec: spec.md. Read it before reviewing."],
        )
        self.assertEqual(list(self.state_dir.glob("*-spec.md")), [])

    def test_an_unfetched_spec_names_no_file(self):
        self.fake_gh(returncode=1, stderr="no such issue\n")
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="99999", axis="spec"))

        self.assertEqual(code, 0)
        self.assertIsNone(output["preparation"]["specFile"])
        self.assertEqual(
            output["preparation"]["specFailure"],
            "gh issue view failed: no such issue",
        )
        self.assertEqual(list(self.state_dir.glob("*-spec.md")), [])

    def test_a_spec_file_that_cannot_be_written_degrades_like_an_unfetched_one(self):
        self.fake_gh(stdout=GH_ISSUE_WITH_A_COMMENT)
        self.codex.finish("no findings")
        with mock.patch.object(
            self.bridge.SessionStore,
            "write_spec",
            side_effect=OSError("No space left on device"),
        ):
            code, output = self.run_bridge(self.args(spec="23", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["specSource"], "not fetched: 23"
        )
        self.assertIsNone(output["preparation"]["specFile"])
        self.assertEqual(
            output["preparation"]["specFailure"],
            "spec file could not be written: No space left on device",
        )
        self.assertIn("No space left on device", prompt)

    def test_a_standards_only_review_still_names_the_spec_file(self):
        self.codex.finish("no findings")

        _code, output = self.run_bridge(
            self.args(spec="spec.md", axis="standards")
        )

        self.assertEqual(output["preparation"]["specFile"], "spec.md")

    def test_a_review_given_no_spec_at_all_names_no_spec_file(self):
        self.codex.finish("no findings")

        _code, output = self.run_bridge(
            self.args(spec=None, axis="standards")
        )

        self.assertIn("specFile", output["preparation"])
        self.assertIsNone(output["preparation"]["specFile"])
        self.assertIn("specFailure", output["preparation"])
        self.assertIsNone(output["preparation"]["specFailure"])

    def test_a_state_directory_inside_the_checkout_fails_before_a_pane_opens(self):
        """The Scope itself is wrong, so no review runs over it rather than a weak one.

        A spec written there would be reviewed as work, and round two would
        read round one's report the same way; that is not a spec being
        unreachable, which is the only thing #30 lets a review continue past.
        """
        inside = self.worktree / "state-inside"
        self.fake_gh(stdout=GH_ISSUE_WITH_A_COMMENT)
        self.enter(mock.patch.dict(
            os.environ, {"CODE_REVIEW_TUI_STATE_DIR": str(inside)}, clear=False
        ))

        with self.assertRaisesRegex(
            RuntimeError, "state directory is inside the reviewed checkout"
        ):
            self.run_bridge(self.args(spec="23", axis="spec"))

        self.assertEqual(self.codex.launched_panes, [])
        self.assertEqual(list(inside.glob("*-spec.md")), [])

    def test_an_issue_spec_writes_nothing_into_the_reviewed_checkout(self):
        self.fake_gh(stdout=GH_ISSUE_WITH_A_COMMENT)
        before = self.untracked_files()
        self.codex.finish("no findings")

        self.run_bridge(self.args(spec="23", axis="spec"))

        self.assertEqual(self.untracked_files(), before)

    def test_a_file_spec_writes_nothing_into_the_reviewed_checkout(self):
        before = self.untracked_files()
        self.codex.finish("no findings")

        self.run_bridge(self.args(spec="spec.md", axis="spec"))

        self.assertEqual(self.untracked_files(), before)

    def test_a_gh_binary_that_cannot_run_reports_the_process_failure(self):
        self.fake_gh(error=OSError("gh is unavailable"))
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="23", axis="spec"))

        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["specFailure"],
            "gh could not be run: gh is unavailable",
        )

    def test_unreadable_issue_json_reports_the_complete_failure(self):
        self.fake_gh(raw_stdout="not json")
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="23", axis="spec"))

        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["specFailure"],
            "gh issue view returned output that could not be read",
        )

    def test_a_gh_failure_degrades_the_spec_axis_instead_of_stopping_it(self):
        self.fake_gh(
            returncode=1,
            stderr=(
                "GraphQL: Could not resolve to an issue or pull request with "
                "the number of 99999. (repository.issue)\n"
                "Authenticate with gh auth login.\n"
            ),
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(spec="99999", axis="spec"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["specSource"], "not fetched: 99999"
        )
        self.assertEqual(
            output["preparation"]["specFailure"],
            "gh issue view failed: GraphQL: Could not resolve to an issue or "
            "pull request with the number of 99999. (repository.issue)\n"
            "Authenticate with gh auth login.",
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
        self.assertEqual(
            output["preparation"]["specFailure"],
            "the issue has no body and no comments",
        )
        self.assertIn("the issue has no body and no comments", prompt)


    def install_convention(self, ignored):
        """Put the tracker convention in the checkout, tracked or excluded."""
        write_convention(self.worktree)
        if ignored:
            exclude_convention_directory(self.worktree)
            return
        for arguments in (
            ("add", "-A"),
            ("commit", "--quiet", "-m", "convention"),
        ):
            subprocess.run(
                ["git", "-C", str(self.worktree), *arguments], check=True
            )

    def test_the_receipt_reports_a_tracked_convention_as_a_standards_source(self):
        self.install_convention(ignored=False)
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["standardsFiles"],
            ["AGENTS.md", "docs/agents/issue-tracker.md"],
        )
        self.assertEqual(
            output["preparation"]["standardsCondition"], "all tracked"
        )

    def test_the_receipt_tells_an_ignored_convention_apart_from_a_missing_one(self):
        """What the Lane is briefed with, and what the caller is owed besides (#40)."""
        self.install_convention(ignored=True)
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["standardsFiles"], ["AGENTS.md"])
        self.assertEqual(
            output["preparation"]["standardsCondition"],
            "present but untracked: docs/agents/issue-tracker.md",
        )
        self.assertIn("Standards sources: AGENTS.md\n", prompt)

    def test_the_standards_brief_names_a_convention_declared_local(self):
        """The widened list reaches the Axis Brief through the same slot (#51)."""
        write_convention(self.worktree)
        write_ignore_rules(self.worktree, ("docs/agents",))
        for arguments in (
            ("add", "-A"),
            ("commit", "--quiet", "-m", "declare the convention local"),
        ):
            subprocess.run(
                ["git", "-C", str(self.worktree), *arguments], check=True
            )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(
            output["preparation"]["standardsFiles"],
            ["AGENTS.md", "docs/agents/issue-tracker.md"],
        )
        self.assertEqual(
            output["preparation"]["standardsCondition"], "all tracked"
        )
        self.assertIn(
            "Standards sources: AGENTS.md, docs/agents/issue-tracker.md\n",
            prompt,
        )


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
