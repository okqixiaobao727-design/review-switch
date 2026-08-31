#!/usr/bin/env python3
"""Document Review behaviour observed at the Bridge command seam."""

import contextlib
import io
import json
import subprocess
import unittest
import uuid
from unittest import mock

from bridge_harness import FakePaneTestCase, assert_first_round_turns


REQUIREMENTS_BRIEF_WITHOUT_PARENT = """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Parent: not provided; hold the documents to each other.
Documents:
- docs/spec.md
- docs/ticket.md

Template baseline (the rules these documents were written under; every item is a judgement call; report only what would change what gets built — not wording, format, or hypothetical future needs):
- Vertical slice: a ticket cuts one complete path through every layer and is demoable or verifiable on its own. → a ticket that is one layer of several is recut.
- Blocking edges: a ticket is blocked only by the tickets that genuinely gate it. → an edge that does not gate is removed; a gate that is missing is added.
- Acceptance criteria: written from the user's perspective, each verifying the behaviour "What to build" claims. → a criterion that checks something else, or nothing observable, is rewritten.
- One context window: a ticket is sized to finish in a single fresh session. → split it.
- No parent (Document Review's own rule): a spec's Problem Statement, Solution, User Stories, Implementation Decisions, Testing Decisions, and Out of Scope are held to one another. → the section that contradicts the others is the finding.

Report: (a) requirements in the parent that the documents miss or carry only in part; (b) scope or decisions the parent never asked for; (c) acceptance criteria that do not verify what their own document claims. With no parent, hold the documents to each other. Quote the line for each finding. Under 400 words."""


DESIGN_BRIEF_WITHOUT_PARENT = """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Load the codebase-design skill from the mattpocock-skills plugin before reviewing and judge in its vocabulary; if you cannot load it, say so in the first line of your report.
Parent: not provided; hold the documents to each other.
Documents:
- docs/spec.md
- docs/ticket.md
Standards sources: AGENTS.md

Template baseline (the rules these documents were written under; every item is a judgement call; report only what would change what gets built — not wording, format, or hypothetical future needs):
- Seams: existing seams are preferred to new ones, the highest possible, and as few as possible. → a new seam says why no existing one serves.
- Testing decisions test external behaviour through the interface, not implementation. → a test plan that must cross the interface is a finding about the module's shape.
- The glossary's vocabulary and the ADRs are followed. → name the term or the ADR.
- No file paths or code snippets, except a prototype's snippet that carries a decision. → cut it.

Report: (a) a Module the design adds that an existing one already owns; (b) a Seam or Interface the design adds where the checkout already has one; (c) a decision that contradicts an ADR or the glossary. Name the code or ADR beside the quoted line for each finding. Under 400 words."""


def briefs_with_parent(parent_line, document_lines):
    without_parent = (
        "Parent: not provided; hold the documents to each other.\n"
        "Documents:\n"
        "- docs/spec.md\n"
        "- docs/ticket.md"
    )
    with_parent = (
        f"{parent_line}\nDocuments:\n"
        + "\n".join(f"- {line}" for line in document_lines)
    )
    return [
        REQUIREMENTS_BRIEF_WITHOUT_PARENT.replace(
            without_parent, with_parent
        ),
        DESIGN_BRIEF_WITHOUT_PARENT.replace(without_parent, with_parent),
    ]


class DocumentReviewCommandTests(FakePaneTestCase):
    def assert_delivered_briefs(self, templates):
        """Every turn this call delivered, against the Axis Briefs it filled."""
        assert_first_round_turns(
            self,
            [
                turn["input"][0]["text"].split("\n", 1)[1]
                for turn in self.codex.started_turns
            ],
            templates,
        )

    def test_documents_without_a_parent_deliver_both_briefs_and_the_receipt(self):
        documents = ("docs/spec.md", "docs/ticket.md")
        for document in documents:
            path = self.worktree / document
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{document}\n", encoding="utf-8")
        args = self.parsed_args([
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--document", documents[0],
            "--document", documents[1],
            "--axis", "both",
            "--no-network",
        ])
        self.codex.finish("requirements report", axis="requirements")
        self.codex.finish("design report", axis="design")

        code, output = self.run_bridge(args)

        self.assertEqual(code, 0, output)
        self.assertEqual(list(output["axes"]), ["requirements", "design"])
        self.assertEqual(
            [output["axes"][axis]["next"] for axis in output["axes"]],
            ["fix then one re-review", "fix then one re-review"],
        )
        self.assert_delivered_briefs(
            [REQUIREMENTS_BRIEF_WITHOUT_PARENT, DESIGN_BRIEF_WITHOUT_PARENT]
        )
        self.assertEqual(
            output["preparation"],
            {
                "parentSource": "not provided",
                "parentFile": None,
                "parentFailure": None,
                "documents": [
                    {"source": "docs/spec.md", "file": "docs/spec.md"},
                    {"source": "docs/ticket.md", "file": "docs/ticket.md"},
                ],
                "standardsFiles": ["AGENTS.md"],
                "standardsCondition": "absent",
                "codeGraphUsed": False,
                "responseFile": None,
            },
        )

    def test_issue_parent_and_document_reach_both_briefs_as_named_files(self):
        issue = json.dumps({
            "body": "Parent body.",
            "comments": [{
                "author": {"login": "owner"},
                "body": "Parent comment.",
            }],
            "number": 43,
            "title": "Parent",
        })
        self.fake_gh(stdout=issue)
        self.enter(mock.patch.object(
            self.bridge.uuid,
            "uuid4",
            side_effect=[uuid.UUID(int=value) for value in range(1, 7)],
        ))
        args = self.parsed_args([
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--parent", "#43",
            "--document", "#45",
            "--axis", "both",
            "--no-network",
        ])
        self.codex.finish("requirements report", axis="requirements")
        self.codex.finish("design report", axis="design")

        code, output = self.run_bridge(args)

        parent_file = str(
            self.state_dir / f"{uuid.UUID(int=1)}-spec.md"
        )
        document_file = str(
            self.state_dir / f"{uuid.UUID(int=2)}-spec.md"
        )
        issue_summary = "#43 Parent, body and 1 comment"
        self.assertEqual(code, 0, output)
        self.assert_delivered_briefs(
            briefs_with_parent(
                f"Parent: {parent_file} — {issue_summary}. "
                "Read it before reviewing.",
                [f"{document_file} — {issue_summary}"],
            )
        )
        self.assertEqual(
            output["preparation"],
            {
                "parentSource": "#43",
                "parentFile": parent_file,
                "parentFailure": None,
                "documents": [
                    {"source": "#45", "file": document_file},
                ],
                "standardsFiles": ["AGENTS.md"],
                "standardsCondition": "absent",
                "codeGraphUsed": False,
                "responseFile": None,
            },
        )

    def test_unfetched_issue_parent_is_carried_by_both_briefs_and_receipt(self):
        self.fake_gh(returncode=1, stderr="issue lookup failed")
        document = self.worktree / "docs/ticket.md"
        document.parent.mkdir(parents=True)
        document.write_text("Ticket.\n", encoding="utf-8")
        args = self.parsed_args([
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--parent", "#404",
            "--document", "docs/ticket.md",
            "--axis", "both",
            "--no-network",
        ])
        self.codex.finish("requirements report", axis="requirements")
        self.codex.finish("design report", axis="design")

        code, output = self.run_bridge(args)

        parent_line = (
            "Parent: not fetched: #404. Failure: "
            "gh issue view failed: issue lookup failed. "
            "Hold the documents to each other."
        )
        self.assertEqual(code, 0, output)
        self.assert_delivered_briefs(
            briefs_with_parent(parent_line, ["docs/ticket.md"])
        )
        self.assertEqual(
            output["preparation"],
            {
                "parentSource": "not fetched: #404",
                "parentFile": None,
                "parentFailure": (
                    "gh issue view failed: issue lookup failed"
                ),
                "documents": [
                    {"source": "docs/ticket.md", "file": "docs/ticket.md"},
                ],
                "standardsFiles": ["AGENTS.md"],
                "standardsCondition": "absent",
                "codeGraphUsed": False,
                "responseFile": None,
            },
        )

    def test_unfetched_path_parent_is_carried_by_both_briefs_and_receipt(self):
        document = self.worktree / "docs/ticket.md"
        document.parent.mkdir(parents=True)
        document.write_text("Ticket.\n", encoding="utf-8")
        args = self.parsed_args([
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--parent", "docs/missing-parent.md",
            "--document", "docs/ticket.md",
            "--axis", "both",
            "--no-network",
        ])
        self.codex.finish("requirements report", axis="requirements")
        self.codex.finish("design report", axis="design")

        code, output = self.run_bridge(args)

        parent_line = (
            "Parent: not fetched: docs/missing-parent.md. "
            "Failure: spec file not found. Hold the documents to each other."
        )
        self.assertEqual(code, 0, output)
        self.assert_delivered_briefs(
            briefs_with_parent(parent_line, ["docs/ticket.md"])
        )
        self.assertEqual(
            output["preparation"],
            {
                "parentSource": "not fetched: docs/missing-parent.md",
                "parentFile": None,
                "parentFailure": "spec file not found",
                "documents": [
                    {"source": "docs/ticket.md", "file": "docs/ticket.md"},
                ],
                "standardsFiles": ["AGENTS.md"],
                "standardsCondition": "absent",
                "codeGraphUsed": False,
                "responseFile": None,
            },
        )

    def test_design_brief_keeps_none_documented_when_no_standards_are_tracked(self):
        subprocess.run(
            ["git", "-C", str(self.worktree), "rm", "AGENTS.md"],
            check=True,
            capture_output=True,
            text=True,
        )
        document = self.worktree / "docs/ticket.md"
        document.parent.mkdir(parents=True)
        document.write_text("Ticket.\n", encoding="utf-8")
        args = self.parsed_args([
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--document", "docs/ticket.md",
            "--axis", "design",
            "--no-network",
        ])
        self.codex.finish("design report", axis="design")

        code, output = self.run_bridge(args)

        expected_brief = DESIGN_BRIEF_WITHOUT_PARENT.replace(
            "- docs/spec.md\n- docs/ticket.md",
            "- docs/ticket.md",
        ).replace(
            "Standards sources: AGENTS.md",
            "Standards sources: none documented; baseline only",
        )
        self.assertEqual(code, 0, output)
        self.assert_delivered_briefs([expected_brief])
        self.assertEqual(output["preparation"]["standardsFiles"], [])
        self.assertEqual(output["preparation"]["standardsCondition"], "absent")

    def test_document_with_a_code_review_reference_names_the_mix(self):
        for option, value in (
            ("--base", self.fixed_point),
            ("--spec", "spec.md"),
        ):
            with self.subTest(option=option):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaisesRegex(SystemExit, "2"):
                        self.bridge.parse_args([
                            "--reviewer", "codex",
                            "--cwd", str(self.worktree),
                            "--document", "docs/ticket.md",
                            option, value,
                        ])

                self.assertEqual(
                    stderr.getvalue().splitlines()[-1].partition(
                        ": error: "
                    )[2],
                    f"--document cannot be combined with {option}",
                )
                self.assertEqual(self.codex.launched_panes, [])

    def test_document_with_a_code_axis_names_the_mix(self):
        for axis in ("standards", "spec"):
            with self.subTest(axis=axis):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaisesRegex(SystemExit, "2"):
                        self.bridge.parse_args([
                            "--reviewer", "codex",
                            "--cwd", str(self.worktree),
                            "--document", "docs/ticket.md",
                            "--axis", axis,
                        ])

                self.assertEqual(
                    stderr.getvalue().splitlines()[-1].partition(
                        ": error: "
                    )[2],
                    f"--axis {axis} is a code axis and cannot be used with "
                    "--document",
                )
                self.assertEqual(self.codex.launched_panes, [])

    def test_help_lists_document_review_options_and_all_public_axes(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaisesRegex(SystemExit, "0"):
                self.bridge.parse_args(["--help"])

        help_text = stdout.getvalue()
        self.assertIn("--parent PARENT", help_text)
        self.assertIn("--document DOCUMENT", help_text)
        self.assertIn(
            "--axis {standards,spec,requirements,design,both}", help_text
        )
        self.assertEqual(self.codex.launched_panes, [])


if __name__ == "__main__":
    unittest.main()
