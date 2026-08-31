#!/usr/bin/env python3
"""The Rounds Contract: the cap one lineage earns, and what every result says next.

This is the cap's one home in the suite. No other test file asserts how many
rounds a lineage earns or what a result names as the next permitted action.
"""

import os
import pathlib
import unittest

from bridge_harness import FakePaneTestCase


FIX_AND_STOP = "fix and stop"
FIX_THEN_ONE_RE_REVIEW = "fix then one re-review"
ESCALATE = "escalate"
DONE = "done"
RUN_AGAIN = "run again"
REFUSED = "refused"
RESPONSE_FORMAT = (
    "N. <short quote from the finding> — fixed <where> | declined <why> | "
    "deferred <ticket>"
)
# Every field a refused resume's axis result carries, and no other: the Bridge
# names the moment a caller must escalate at and never the act of escalating.
REFUSAL_FIELDS = {
    "status", "finalMessage", "reviewSessionId", "reason", "next", "nextCall"
}
LANES = ("codex", "claude")


class RoundsContractTestCase(FakePaneTestCase):
    """A review lineage driven round by round, on either Lane."""

    def review(self, reviewer, axis, message):
        """Round one of a lineage: one axis reviewed and reported on."""
        self.lane(reviewer).finish(message, axis=axis)
        code, output = self.run_bridge(self.args(reviewer=reviewer, axis=axis))
        self.assertEqual(code, 0, output)
        return output["axes"][axis]

    def resume(self, reviewer, axis, session, message):
        """One more round put to the lineage a handle names."""
        self.lane(reviewer).finish(message, axis=axis)
        return self.run_bridge(
            self.args(reviewer=reviewer, axis=axis, resume_session=session)
        )


class CodeReviewRoundCharacterizationTests(FakePaneTestCase):
    """Code Review's rounds and Next Call through the command entry."""

    def review_argv(self, axis="both"):
        return [
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--base", self.fixed_point,
            "--spec", "spec.md",
            "--axis", axis,
            "--no-network",
        ]

    def response_path(self, session):
        path = self.state_dir / f"{session}-response.md"
        path.write_text(
            '1. "the characterization finding" — fixed in feature.py\n',
            encoding="utf-8",
        )
        return str(path)

    def test_code_review_rounds_are_pinned_through_the_command_entry(self):
        self.use_graphless_path()
        args = self.parsed_args(self.review_argv())
        self.codex.finish("standards characterization", axis="standards")
        self.codex.finish("spec characterization", axis="spec")

        code, output = self.run_bridge(args)

        self.assertEqual(code, 0, output)
        standards = output["axes"]["standards"]
        self.assertEqual(standards["next"], FIX_AND_STOP)
        self.assertIsNone(standards["nextCall"])
        spec = output["axes"]["spec"]
        self.assertEqual(spec["next"], FIX_THEN_ONE_RE_REVIEW)
        response_file = str(
            self.state_dir / f"{spec['reviewSessionId']}-response.md"
        )
        self.assertEqual(
            spec["nextCall"],
            {
                "argv": [
                    "review-bridge",
                    *args.caller_arguments,
                    "--axis", "spec",
                    "--resume-session", spec["reviewSessionId"],
                    "--response", response_file,
                ],
                "responseFile": response_file,
                "responseFormat": RESPONSE_FORMAT,
            },
        )

        standards_response = self.response_path(standards["reviewSessionId"])
        standards_resume = self.parsed_args([
            *self.review_argv(axis="standards"),
            "--resume-session", standards["reviewSessionId"],
            "--response", standards_response,
        ])
        refused_code, refused = self.run_bridge(standards_resume)
        self.assertEqual(refused_code, 1, refused)
        self.assertEqual(
            refused["axes"]["standards"]["reason"],
            "a standards axis earns 1 round(s) per review lineage, and this one has had 1",
        )

        pathlib.Path(response_file).write_text(
            '1. "spec characterization" — fixed in feature.py\n',
            encoding="utf-8",
        )
        spec_resume = self.parsed_args(spec["nextCall"]["argv"][1:])
        self.codex.finish("spec re-review characterization", axis="spec")
        resumed_code, resumed = self.run_bridge(spec_resume)
        self.assertEqual(resumed_code, 0, resumed)
        self.assertEqual(resumed["axes"]["spec"]["next"], ESCALATE)
        self.assertIsNone(resumed["axes"]["spec"]["nextCall"])

        spent_resume = self.parsed_args(spec["nextCall"]["argv"][1:])
        spent_code, spent = self.run_bridge(spent_resume)
        self.assertEqual(spent_code, 1, spent)
        self.assertEqual(
            spent["axes"]["spec"]["reason"],
            "a spec axis earns 2 round(s) per review lineage, and this one has had 2",
        )


class DocumentReviewRoundCharacterizationTests(FakePaneTestCase):
    """Document Review's rounds and Next Call through the command entry."""

    def test_each_document_axis_round_trips_its_re_review_call_on_both_lanes(self):
        for name in ("parent.md", "first.md", "second.md"):
            path = self.worktree / "docs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{name}\n", encoding="utf-8")

        for reviewer in LANES:
            for axis in ("requirements", "design"):
                with self.subTest(reviewer=reviewer, axis=axis):
                    caller_arguments = [
                        "--reviewer", reviewer,
                        "--cwd", str(self.worktree),
                        "--parent", "docs/parent.md",
                        "--document", "docs/first.md",
                        "--document", "docs/second.md",
                        "--no-network",
                    ]
                    first_args = self.parsed_args([
                        *caller_arguments,
                        "--axis", axis,
                    ])
                    self.lane(reviewer).finish(
                        f"{axis} round one findings",
                        axis=axis,
                    )

                    first_code, first_output = self.run_bridge(first_args)

                    self.assertEqual(first_code, 0, first_output)
                    first_result = first_output["axes"][axis]
                    session = first_result["reviewSessionId"]
                    response_file = str(
                        self.state_dir / f"{session}-response.md"
                    )
                    self.assertEqual(first_result["next"], FIX_THEN_ONE_RE_REVIEW)
                    self.assertEqual(
                        first_result["nextCall"],
                        {
                            "argv": [
                                "review-bridge",
                                *caller_arguments,
                                "--axis", axis,
                                "--resume-session", session,
                                "--response", response_file,
                            ],
                            "responseFile": response_file,
                            "responseFormat": RESPONSE_FORMAT,
                        },
                    )
                    pathlib.Path(response_file).write_text(
                        f'1. "{axis} round one findings" — fixed in docs/first.md\n',
                        encoding="utf-8",
                    )
                    resumed_args = self.parsed_args(
                        first_result["nextCall"]["argv"][1:]
                    )
                    self.lane(reviewer).finish(
                        f"{axis} round two findings",
                        axis=axis,
                    )

                    resumed_code, resumed_output = self.run_bridge(resumed_args)

                    self.assertEqual(resumed_code, 0, resumed_output)
                    resumed_result = resumed_output["axes"][axis]
                    self.assertEqual(
                        resumed_result["finalMessage"],
                        f"{axis} round two findings",
                    )
                    self.assertEqual(resumed_result["next"], ESCALATE)
                    self.assertIsNone(resumed_result["nextCall"])


class NextActionTests(RoundsContractTestCase):
    """Every result names the one action its caller is permitted next."""

    def test_a_standards_result_says_fix_and_stop(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                result = self.review(reviewer, "standards", "naming findings")

                self.assertEqual(result["next"], FIX_AND_STOP)
                self.assertIsNone(result["nextCall"])

    def test_a_first_spec_result_says_fix_then_one_re_review(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                result = self.review(reviewer, "spec", "one spec finding")

                self.assertEqual(result["next"], FIX_THEN_ONE_RE_REVIEW)

    def test_a_first_spec_result_carries_the_re_review_call(self):
        caller_arguments = [
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--base", self.fixed_point,
            "--spec", "spec.md",
            "--no-network",
            "--on-review-start", "notify first\nnotify second",
        ]
        self.codex.finish("one spec finding", axis="spec")

        code, output = self.run_bridge(
            self.args(
                axis="spec",
                network=False,
                caller_arguments=caller_arguments,
            )
        )

        self.assertEqual(code, 0, output)
        result = output["axes"]["spec"]
        session = result["reviewSessionId"]
        response_file = str(self.state_dir / f"{session}-response.md")
        self.assertEqual(
            result["nextCall"],
            {
                "argv": [
                    "review-bridge",
                    *caller_arguments,
                    "--axis", "spec",
                    "--resume-session", session,
                    "--response", response_file,
                ],
                "responseFile": response_file,
                "responseFormat": RESPONSE_FORMAT,
            },
        )

    def test_the_re_review_call_round_trips_through_the_bridge(self):
        first_argv = [
            "--rev", "codex",
            "--cw", str(self.worktree),
            "--bas", self.fixed_point,
            "--spec", "spec.md",
            "--axis", "spec",
            "--no-network",
        ]
        first_args = self.bridge.parse_args(first_argv)
        first_args.status = "failed"
        first_args.resume_state = None
        self.codex.finish("one spec finding", axis="spec")
        first_code, first_output = self.run_bridge(first_args)
        self.assertEqual(first_code, 0, first_output)
        next_call = first_output["axes"]["spec"]["nextCall"]
        response_file = pathlib.Path(next_call["responseFile"])
        response_text = '1. "one spec finding" — fixed in feature.py\n'
        response_file.write_text(response_text, encoding="utf-8")

        resumed_args = self.bridge.parse_args(next_call["argv"][1:])

        self.assertEqual(resumed_args.reviewer, "codex")
        self.assertEqual(resumed_args.base, self.fixed_point)
        self.assertEqual(resumed_args.spec, "spec.md")
        self.assertEqual(resumed_args.axis, "spec")
        self.assertFalse(resumed_args.network)
        self.assertEqual(resumed_args.response, str(response_file))
        resumed_args.status = "failed"
        resumed_args.resume_state = self.bridge.resume_state_for_review(resumed_args)
        self.codex.finish("the fix closes it", axis="spec")

        resumed_code, resumed_output = self.run_bridge(resumed_args)

        self.assertEqual(resumed_code, 0, resumed_output)
        self.assertEqual(
            resumed_output["axes"]["spec"]["finalMessage"],
            "the fix closes it",
        )
        self.assertIn(
            response_text,
            self.codex.started_turns[-1]["input"][0]["text"],
        )
        self.assertEqual(
            [
                path.resolve()
                for path in self.state_dir.glob(
                    f"{resumed_args.resume_session}-response*.md"
                )
            ],
            [response_file.resolve()],
        )

    def test_a_spec_re_review_that_counted_nothing_says_done(self):
        """The rounds are spent and the reviewer counted nothing: no escalation."""
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "spec", "one spec finding")

                code, output = self.resume(
                    reviewer,
                    "spec",
                    first["reviewSessionId"],
                    "fix closes it\n\nRetained: 0; New: 0",
                )

                self.assertEqual(code, 0, output)
                result = output["axes"]["spec"]
                self.assertEqual(result["next"], DONE)
                self.assertIsNone(result["nextCall"])
                self.assertEqual(result["findings"], {"retained": 0, "new": 0})

    def test_a_two_axis_review_names_each_axis_its_own_action(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                lane = self.lane(reviewer)
                lane.finish("naming findings", axis="standards")
                lane.finish("one spec finding", axis="spec")

                code, output = self.run_bridge(
                    self.args(reviewer=reviewer, axis="both")
                )

                self.assertEqual(code, 0, output)
                self.assertEqual(
                    {
                        axis: result["next"]
                        for axis, result in output["axes"].items()
                    },
                    {
                        "standards": FIX_AND_STOP,
                        "spec": FIX_THEN_ONE_RE_REVIEW,
                    },
                )

    def test_a_failed_axis_carries_a_fresh_single_axis_call_on_both_lanes(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                caller_arguments = [
                    "--reviewer", reviewer,
                    "--cwd", str(self.worktree),
                    "--base", self.fixed_point,
                    "--spec", "spec.md",
                ]
                self.lane(reviewer).error("spec", "the reviewer went away")

                code, output = self.run_bridge(
                    self.args(
                        reviewer=reviewer,
                        axis="spec",
                        caller_arguments=caller_arguments,
                    )
                )

                self.assertEqual(code, 1, output)
                result = output["axes"]["spec"]
                self.assertEqual(result["next"], RUN_AGAIN)
                self.assertEqual(
                    result["nextCall"],
                    {
                        "argv": [
                            "review-bridge",
                            *caller_arguments,
                            "--axis", "spec",
                        ],
                        "responseFile": None,
                        "responseFormat": None,
                    },
                )

    def test_an_empty_final_message_is_run_again_on_both_lanes(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                caller_arguments = [
                    "--reviewer", reviewer,
                    "--cwd", str(self.worktree),
                    "--base", self.fixed_point,
                    "--spec", "spec.md",
                ]
                self.lane(reviewer).finish("", axis="spec")

                code, output = self.run_bridge(
                    self.args(
                        reviewer=reviewer,
                        axis="spec",
                        caller_arguments=caller_arguments,
                    )
                )

                self.assertEqual(code, 1, output)
                result = output["axes"]["spec"]
                self.assertEqual(result["next"], RUN_AGAIN)
                self.assertEqual(
                    result["nextCall"]["argv"],
                    ["review-bridge", *caller_arguments, "--axis", "spec"],
                )
                self.assertIsNone(result["nextCall"]["responseFile"])
                self.assertIsNone(result["nextCall"]["responseFormat"])

    def test_a_failed_axis_in_a_both_review_names_only_its_fresh_call(self):
        caller_arguments = [
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--base", self.fixed_point,
            "--spec", "spec.md",
        ]
        self.codex.finish("standards findings", axis="standards")
        self.codex.error("spec", "the reviewer went away")

        code, output = self.run_bridge(
            self.args(axis="both", caller_arguments=caller_arguments)
        )

        self.assertEqual(code, 1, output)
        self.assertEqual(output["axes"]["standards"]["next"], FIX_AND_STOP)
        self.assertIsNone(output["axes"]["standards"]["nextCall"])
        spec = output["axes"]["spec"]
        self.assertEqual(spec["next"], RUN_AGAIN)
        self.assertEqual(
            spec["nextCall"]["argv"],
            ["review-bridge", *caller_arguments, "--axis", "spec"],
        )


class RoundCapTests(RoundsContractTestCase):
    """A resume past the cap is refused, whoever asks and however they ask."""

    def assert_refused(self, code, output, axis, session):
        """The one shape a refusal takes: no review ran, and escalate is next."""
        self.assertEqual(code, 1, output)
        self.assertEqual(output["status"], REFUSED)
        self.assertIsNone(output["preparation"])
        self.assertEqual(set(output["axes"]), {axis})
        result = output["axes"][axis]
        self.assertEqual(result["status"], REFUSED)
        self.assertEqual(result["next"], ESCALATE)
        self.assertIsNone(result["nextCall"])
        self.assertEqual(result["reviewSessionId"], session)
        self.assertEqual(set(result), REFUSAL_FIELDS)

    def test_a_standards_axis_is_refused_any_resume(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "standards", "naming findings")
                session = first["reviewSessionId"]
                delivered = len(self.delivered(reviewer))

                code, output = self.resume(
                    reviewer, "standards", session, "round two"
                )

                self.assert_refused(code, output, "standards", session)
                self.assertEqual(len(self.delivered(reviewer)), delivered)

    def test_a_spec_axis_is_granted_one_resume_and_refused_a_second(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "spec", "one spec finding")
                session = first["reviewSessionId"]

                granted, _first_output = self.resume(
                    reviewer, "spec", session, "fix closes it"
                )
                delivered = len(self.delivered(reviewer))
                refused, output = self.resume(
                    reviewer, "spec", session, "round three"
                )

                self.assertEqual(granted, 0)
                self.assert_refused(refused, output, "spec", session)
                self.assertEqual(len(self.delivered(reviewer)), delivered)

    def test_a_fresh_review_is_unaffected_by_a_lineage_that_reached_its_cap(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                capped = self.review(reviewer, "spec", "one spec finding")
                self.resume(
                    reviewer, "spec", capped["reviewSessionId"], "fix closes it"
                )
                refused, _output = self.resume(
                    reviewer, "spec", capped["reviewSessionId"], "round three"
                )
                self.assertEqual(refused, 1)

                fresh = self.review(reviewer, "spec", "a fresh first round")

                self.assertNotEqual(
                    fresh["reviewSessionId"], capped["reviewSessionId"]
                )
                self.assertEqual(fresh["finalMessage"], "a fresh first round")
                self.assertEqual(fresh["next"], FIX_THEN_ONE_RE_REVIEW)

    def test_a_resume_that_fails_has_still_had_its_round(self):
        """A round is spent when it is granted, not when it succeeds."""
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "spec", "one spec finding")
                session = first["reviewSessionId"]
                self.lane(reviewer).error("spec", "the reviewer went away")

                failed, failed_output = self.resume(
                    reviewer, "spec", session, "never arrives"
                )

                self.assertEqual(failed, 1)
                self.assertEqual(
                    failed_output["axes"]["spec"]["next"], RUN_AGAIN
                )
                self.assertNotIn(
                    "--resume-session",
                    failed_output["axes"]["spec"]["nextCall"]["argv"],
                )

                del self.lane(reviewer).axis_errors["spec"]
                code, output = self.resume(
                    reviewer, "spec", session, "round three"
                )

                self.assert_refused(code, output, "spec", session)

    def test_a_resume_refused_by_a_lock_spends_no_round(self):
        """A collision is not a round: the cap sits where it would have sat."""
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "spec", "one spec finding")
                session = first["reviewSessionId"]
                store = self.bridge.SessionStore()
                owner = self.bridge.resolve_lane(
                    self.args(reviewer=reviewer, axis="spec"), store
                ).owner

                with self.bridge.owner_lock(store, owner, session):
                    with self.assertRaises(self.bridge.LockRefusedError):
                        self.resume(reviewer, "spec", session, "collides")

                granted, _output = self.resume(
                    reviewer, "spec", session, "fix closes it"
                )
                code, output = self.resume(
                    reviewer, "spec", session, "round three"
                )

                self.assertEqual(granted, 0)
                self.assertEqual(store.read(session)["rounds"], 2)
                self.assert_refused(code, output, "spec", session)

    def test_a_handle_read_before_a_sibling_round_cannot_take_that_round_again(self):
        """The copy a caller arrived with is not what the cap is decided on."""
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "spec", "one spec finding")
                session = first["reviewSessionId"]
                # What a caller holding a handle read before round two ran has.
                stale = self.bridge.SessionStore().read(session)

                granted, _output = self.resume_holding(
                    reviewer, session, stale, "fix closes it"
                )
                code, output = self.resume_holding(
                    reviewer, session, stale, "round three"
                )

                self.assertEqual(granted, 0)
                self.assert_refused(code, output, "spec", session)

    def test_a_capped_handle_of_another_owner_says_nothing_about_its_rounds(self):
        """Whose session it is is settled before its rounds are spoken of."""
        first = self.review("codex", "spec", "one spec finding")
        session = first["reviewSessionId"]
        self.resume("codex", "spec", session, "fix closes it")
        os.environ["TMUX_PANE"] = "%777"

        with self.assertRaisesRegex(RuntimeError, "another tmux pane"):
            self.resume("codex", "spec", session, "round three")

    def resume_holding(self, reviewer, session, state, message):
        """One more round put to a lineage by a caller holding `state` already."""
        self.lane(reviewer).finish(message, axis="spec")
        return self.run_bridge(
            self.args(
                reviewer=reviewer,
                axis="spec",
                resume_session=session,
                resume_state=dict(state),
            )
        )

    def delivered(self, reviewer):
        """Every brief this Lane's reviewer has been handed so far."""
        if reviewer == "claude":
            return self.claude.launched
        return self.codex.started_turns


class VerdictLineTests(RoundsContractTestCase):
    """The counts the reviewer ends its report with, and what they settle."""

    def test_a_re_review_with_a_new_finding_escalates(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "spec", "one spec finding")

                code, output = self.resume(
                    reviewer,
                    "spec",
                    first["reviewSessionId"],
                    "the fix broke something\n\nRetained: 0, New: 1",
                )

                self.assertEqual(code, 0, output)
                result = output["axes"]["spec"]
                self.assertEqual(result["next"], ESCALATE)
                self.assertIsNone(result["nextCall"])
                self.assertEqual(result["findings"], {"retained": 0, "new": 1})

    def test_a_re_review_with_a_retained_finding_escalates(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "spec", "two spec findings")

                code, output = self.resume(
                    reviewer,
                    "spec",
                    first["reviewSessionId"],
                    "both stand\n\nRetained: 2; New: 0",
                )

                self.assertEqual(code, 0, output)
                result = output["axes"]["spec"]
                self.assertEqual(result["next"], ESCALATE)
                self.assertEqual(result["findings"], {"retained": 2, "new": 0})

    def test_a_first_spec_round_that_counted_nothing_says_done(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                result = self.review(
                    reviewer, "spec", "nothing to report\n\nFindings: 0"
                )

                self.assertEqual(result["next"], DONE)
                self.assertIsNone(result["nextCall"])
                self.assertEqual(result["findings"], {"reported": 0})

    def test_a_first_spec_round_that_counted_findings_keeps_its_re_review(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                result = self.review(
                    reviewer, "spec", "three problems\n\nFindings: 3"
                )

                self.assertEqual(result["next"], FIX_THEN_ONE_RE_REVIEW)
                self.assertEqual(result["findings"], {"reported": 3})
                session = result["reviewSessionId"]
                response_file = str(
                    self.state_dir / f"{session}-response.md"
                )
                self.assertEqual(
                    result["nextCall"]["argv"][-4:],
                    [
                        "--resume-session", session,
                        "--response", response_file,
                    ],
                )
                self.assertEqual(
                    result["nextCall"]["responseFile"], response_file
                )
                self.assertEqual(
                    result["nextCall"]["responseFormat"], RESPONSE_FORMAT
                )

    def test_a_standards_round_that_counted_nothing_says_done(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                result = self.review(
                    reviewer, "standards", "clean\n\nFindings: 0"
                )

                self.assertEqual(result["next"], DONE)
                self.assertIsNone(result["nextCall"])
                self.assertEqual(result["findings"], {"reported": 0})

    def test_a_standards_round_that_counted_one_still_says_fix_and_stop(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                result = self.review(
                    reviewer, "standards", "one naming finding\n\nFindings: 1"
                )

                self.assertEqual(result["next"], FIX_AND_STOP)
                self.assertEqual(result["findings"], {"reported": 1})

    def test_a_report_without_a_verdict_line_carries_no_findings(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                result = self.review(reviewer, "spec", "one spec finding")

                self.assertIsNone(result["findings"])
                self.assertEqual(result["next"], FIX_THEN_ONE_RE_REVIEW)

    def test_only_the_last_verdict_line_is_read(self):
        result = self.review(
            "codex",
            "spec",
            "Findings: 3 was the first pass\nthen I closed them\nFindings: 0",
        )

        self.assertEqual(result["findings"], {"reported": 0})
        self.assertEqual(result["next"], DONE)

    def test_a_verdict_line_with_prose_after_it_is_not_the_last_line(self):
        """The turn asks for a line with nothing after it, and means it."""
        result = self.review(
            "codex", "spec", "Findings: 0\n\nOne more thought, though."
        )

        self.assertIsNone(result["findings"])
        self.assertEqual(result["next"], FIX_THEN_ONE_RE_REVIEW)

    def test_a_failed_axis_that_still_printed_a_count_carries_no_findings(self):
        """What a review never finished saying is not the review's answer."""
        self.claude.answer_with(
            {
                "session_id": "claude-spec",
                "result": "I cannot run this review.\n\nFindings: 0",
                "is_error": True,
                "subtype": "error_during_execution",
                "permission_denials": [],
            },
            axis="spec",
        )

        code, output = self.run_bridge(
            self.args(reviewer="claude", axis="spec")
        )

        self.assertEqual(code, 1, output)
        result = output["axes"]["spec"]
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["findings"])
        self.assertEqual(result["next"], RUN_AGAIN)

    def test_a_malformed_last_verdict_line_yields_no_findings(self):
        result = self.review(
            "codex", "spec", "Findings: 2\n\nFindings: none of them"
        )

        self.assertIsNone(result["findings"])
        self.assertEqual(result["next"], FIX_THEN_ONE_RE_REVIEW)

    def test_a_re_review_is_not_read_by_the_first_round_token(self):
        first = self.review("codex", "spec", "one spec finding")

        code, output = self.resume(
            "codex", "spec", first["reviewSessionId"], "all clear\n\nFindings: 0"
        )

        self.assertEqual(code, 0, output)
        result = output["axes"]["spec"]
        self.assertIsNone(result["findings"])
        self.assertEqual(result["next"], ESCALATE)

    def test_the_verdict_line_is_read_whatever_its_case_and_spacing(self):
        first = self.review("codex", "spec", "one spec finding")

        code, output = self.resume(
            "codex",
            "spec",
            first["reviewSessionId"],
            "closed\n\n  RETAINED:  0 ,  new:0  ",
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(
            output["axes"]["spec"]["findings"], {"retained": 0, "new": 0}
        )
        self.assertEqual(output["axes"]["spec"]["next"], DONE)

    def test_a_failed_axis_carries_no_findings(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                self.lane(reviewer).error("spec", "the reviewer went away")

                code, output = self.run_bridge(
                    self.args(reviewer=reviewer, axis="spec")
                )

                self.assertEqual(code, 1, output)
                result = output["axes"]["spec"]
                self.assertIsNone(result["findings"])
                self.assertEqual(result["next"], RUN_AGAIN)

    def test_every_axis_of_a_completed_review_carries_findings(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                lane = self.lane(reviewer)
                lane.finish("naming findings\n\nFindings: 1", axis="standards")
                lane.finish("one spec finding", axis="spec")

                code, output = self.run_bridge(
                    self.args(reviewer=reviewer, axis="both")
                )

                self.assertEqual(code, 0, output)
                self.assertEqual(
                    {
                        axis: result["findings"]
                        for axis, result in output["axes"].items()
                    },
                    {"standards": {"reported": 1}, "spec": None},
                )


class DocumentReviewVerdictLineTests(FakePaneTestCase):
    """Both document axes answer their own counts through the command entry."""

    def document_argv(self, axis):
        return [
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--parent", "docs/parent.md",
            "--document", "docs/first.md",
            "--axis", axis,
            "--no-network",
        ]

    def write_documents(self):
        for name in ("parent.md", "first.md"):
            path = self.worktree / "docs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{name}\n", encoding="utf-8")

    def test_a_document_first_round_that_counted_nothing_says_done(self):
        self.write_documents()
        for axis in ("requirements", "design"):
            with self.subTest(axis=axis):
                self.codex.finish(f"{axis} is clean\n\nFindings: 0", axis=axis)

                code, output = self.run_bridge(
                    self.parsed_args(self.document_argv(axis))
                )

                self.assertEqual(code, 0, output)
                result = output["axes"][axis]
                self.assertEqual(result["next"], DONE)
                self.assertIsNone(result["nextCall"])
                self.assertEqual(result["findings"], {"reported": 0})


if __name__ == "__main__":
    unittest.main()
