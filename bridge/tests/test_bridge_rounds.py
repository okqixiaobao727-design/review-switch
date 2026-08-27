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

    def lane(self, reviewer):
        """The stub standing in for this Lane's reviewer."""
        return self.claude if reviewer == "claude" else self.codex

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

    def test_the_spec_re_review_result_says_escalate(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first = self.review(reviewer, "spec", "one spec finding")

                code, output = self.resume(
                    reviewer, "spec", first["reviewSessionId"], "fix closes it"
                )

                self.assertEqual(code, 0, output)
                self.assertEqual(output["axes"]["spec"]["next"], ESCALATE)
                self.assertIsNone(output["axes"]["spec"]["nextCall"])

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


if __name__ == "__main__":
    unittest.main()
