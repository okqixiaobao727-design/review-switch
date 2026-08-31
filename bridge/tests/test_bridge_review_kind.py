#!/usr/bin/env python3
"""Code Review behaviour held constant while review kinds become table-driven."""

import contextlib
import io
import subprocess
import sys
import unittest

from bridge_harness import FakePaneTestCase, assert_first_round_turns


class CodeReviewCharacterizationTests(FakePaneTestCase):
    """Code Review's command inputs, Axis Briefs, and receipt."""

    def review_argv(self, axis="both"):
        return [
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--base", self.fixed_point,
            "--spec", "spec.md",
            "--axis", axis,
            "--no-network",
        ]

    def expected_briefs(self):
        head = subprocess.run(
            ["git", "-C", str(self.worktree), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        scope = self.fixed_point
        standards = f"""Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff {scope}
New files not in that diff: git ls-files --others --exclude-standard
Commits:
{head} feature change

Standards sources: AGENTS.md

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

Report, per file/hunk where relevant, (a) every place the diff violates a documented standard: cite the standard (file + the rule); and (b) any baseline smell you spot: name it and quote the hunk. Distinguish hard violations from judgement calls: documented-standard breaches can be hard, but baseline smells are always judgement calls, and a documented repo standard overrides the baseline. Skip anything tooling enforces. Under 400 words."""
        spec = f"""Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff {scope}
New files not in that diff: git ls-files --others --exclude-standard
Commits:
{head} feature change

Spec: spec.md. Read it before reviewing.

Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words."""
        return standards, spec

    def test_code_review_behaviour_is_pinned_through_the_command_entry(self):
        self.use_graphless_path()
        args = self.parsed_args(self.review_argv())
        self.codex.finish("standards characterization", axis="standards")
        self.codex.finish("spec characterization", axis="spec")

        code, output = self.run_bridge(args)

        self.assertEqual(code, 0, output)
        self.assertEqual(args.reviewer, "codex")
        self.assertEqual(args.cwd, str(self.worktree))
        self.assertEqual(args.base, self.fixed_point)
        self.assertEqual(args.spec, "spec.md")
        self.assertEqual(args.axis, "both")
        self.assertFalse(args.network)
        self.assertEqual(
            args.caller_arguments,
            [
                "--reviewer", "codex",
                "--cwd", str(self.worktree),
                "--base", self.fixed_point,
                "--spec", "spec.md",
                "--no-network",
            ],
        )
        self.assertEqual(list(output["axes"]), ["standards", "spec"])
        delivered_briefs = [
            turn["input"][0]["text"].split("\n", 1)[1]
            for turn in self.codex.started_turns
        ]
        assert_first_round_turns(
            self, delivered_briefs, self.expected_briefs()
        )
        self.assertEqual(
            output["preparation"],
            {
                "fixedPoint": self.fixed_point,
                "specSource": "spec.md",
                "specFile": "spec.md",
                "specFailure": None,
                "standardsFiles": ["AGENTS.md"],
                "standardsCondition": "absent",
                "codeGraphUsed": False,
                "responseFile": None,
            },
        )

    def test_a_documents_axis_without_document_names_the_mix(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(SystemExit, "2"):
                self.bridge.parse_args(self.review_argv(axis="requirements"))

        self.assertEqual(
            stderr.getvalue().splitlines()[-1].partition(": error: ")[2],
            "--axis requirements is a documents axis and requires --document",
        )
        self.assertEqual(self.codex.launched_panes, [])

    def test_a_genuinely_unknown_axis_keeps_the_exact_command_refusal(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(SystemExit, "2"):
                self.bridge.parse_args(self.review_argv(axis="security"))

        choices = (
            "standards, spec, requirements, design, both"
            if sys.version_info >= (3, 14)
            else "'standards', 'spec', 'requirements', 'design', 'both'"
        )
        self.assertEqual(
            stderr.getvalue().splitlines()[-1].partition(": error: ")[2],
            "argument --axis: invalid choice: "
            f"'security' (choose from {choices})",
        )
        self.assertEqual(self.codex.launched_panes, [])


if __name__ == "__main__":
    unittest.main()
