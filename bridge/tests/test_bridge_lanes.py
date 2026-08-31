#!/usr/bin/env python3
"""Two Lanes on one protocol: what the claude Lane delivers, and where codex matches."""

import json
import os
import pathlib
import sys
import unittest
from unittest import mock

from bridge_harness import (
    DriverKilled,
    FakePaneTestCase,
    MARKER_PATTERN,
    graph_navigation_result,
)


def first_round_turn(bridge, preparation, axis):
    """One axis's whole first-round turn: its brief, then the Bridge's request.

    The Verdict Line request is the Bridge's, appended after the prepared brief
    and delivered by both Lanes alike, so a turn is still what preparation
    filled plus what the Bridge asked for and nothing a Lane added.
    """
    return f"{preparation.brief_text(axis)}\n\n{bridge.FIRST_ROUND_VERDICT.request}"


def delivered_brief(prompt):
    """The Axis Brief inside a prompt, whichever Lane's prompt it is.

    The codex Lane marks its turn so a recovering caller can find it again; the
    brief is what follows that marker, and on a headless Lane it is the whole
    prompt.
    """
    marker = MARKER_PATTERN.match(prompt)
    return prompt[marker.end():].lstrip("\n") if marker else prompt


class ClaudeDeliveryTests(FakePaneTestCase):
    """A whole review delivered to headless reviewers, and what it reports back."""

    def claude_args(self, **overrides):
        values = {"reviewer": "claude"}
        values.update(overrides)
        return self.args(**values)

    def test_a_standards_run_reports_what_was_prepared(self):
        self.claude.finish("no findings")

        code, output = self.run_bridge(self.claude_args(axis="standards"))

        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(set(output["axes"]), {"standards"})
        result = output["axes"]["standards"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finalMessage"], "no findings")
        self.assertNotIn("reason", result)
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
        state = self.stored_session()
        self.assertEqual(result["reviewSessionId"], state["reviewSessionId"])
        self.assertEqual(state["axis"], "standards")
        self.assertEqual(state["claudeSessionId"], "claude-standards")
        self.assertIsNone(state["model"])
        self.assertIsNone(state["effort"])

    def test_each_requested_axis_gets_one_reviewer_of_its_own(self):
        self.claude.finish("standards clear", axis="standards")
        self.claude.finish("spec clear", axis="spec")

        code, output = self.run_bridge(self.claude_args(axis="both"))

        self.assertEqual(code, 0)
        self.assertEqual(
            [process.axis for process in self.claude.launched],
            ["standards", "spec"],
        )
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"], "standards clear"
        )
        self.assertEqual(output["axes"]["spec"]["finalMessage"], "spec clear")
        states = {
            state["axis"]: state
            for state in self.stored_sessions(expected_count=2)
        }
        self.assertEqual(set(states), {"standards", "spec"})
        for axis in ("standards", "spec"):
            self.assertEqual(
                output["axes"][axis]["reviewSessionId"],
                states[axis]["reviewSessionId"],
            )

    def test_the_prompt_is_the_axis_brief_and_nothing_else(self):
        """No prompt of the Lane's own: no scope block, no round rule, no format."""
        self.claude.finish("standards clear", axis="standards")
        self.claude.finish("spec clear", axis="spec")
        args = self.claude_args(axis="both")

        code, _output = self.run_bridge(args)

        self.assertEqual(code, 0)
        self.assertEqual(
            self.claude.prompts,
            [
                first_round_turn(self.bridge, args.preparation, "standards"),
                first_round_turn(self.bridge, args.preparation, "spec"),
            ],
        )
        for prompt in self.claude.prompts:
            for absent in (
                "Rounds contract",
                "REVIEW VERDICT",
                "Change analysis",
                "one re-review",
                "coordinator",
            ):
                self.assertNotIn(absent, prompt)

    def test_the_lane_makes_no_code_graph_call_of_its_own(self):
        (self.worktree / ".code-review-graph").mkdir()
        feature = {
            "file_path": str(self.worktree / "feature.py"),
            "line_start": 1,
            "line_end": 1,
            "name": "feature",
            "risk_score": 0.93,
        }
        call_log = self.install_graph_stub(
            graph_navigation_result(
                feature,
                risk_score=0.93,
                test_gaps=[],
                context_savings={"estimated_tokens_saved": 1200},
            )
        )
        self.claude.finish("standards clear", axis="standards")
        self.claude.finish("spec clear", axis="spec")

        code, output = self.run_bridge(self.claude_args(axis="both"))

        self.assertEqual(code, 0)
        self.assertTrue(output["preparation"]["codeGraphUsed"])
        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [call["argv"][0] for call in calls].count("detect-changes"),
            1,
            "the Lane looked the change up again after preparation had",
        )
        # Spelt out rather than taken from preparation, which every other
        # assertion here compares against: a heading that named the wrong
        # scope would reach both Lanes and satisfy both sides of that
        # comparison (#34).
        for prompt in self.claude.prompts:
            self.assertIn(
                "Start here (from the code graph; the two commands above "
                "are the full scope):\n"
                "feature.py:1–1  feature",
                prompt,
            )

    def test_an_erroring_reviewer_is_a_failed_axis(self):
        self.claude.answer_with(
            {
                "session_id": "claude-standards",
                "result": "",
                "is_error": True,
                "subtype": "error_during_execution",
                "permission_denials": [],
            },
            axis="standards",
        )

        code, output = self.run_bridge(self.claude_args(axis="standards"))

        self.assertEqual(code, 1)
        self.assertEqual(output["status"], "partially_completed")
        result = output["axes"]["standards"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("error_during_execution", result["reason"])
        self.assertTrue(result["reviewSessionId"])

    def test_a_reviewer_that_returns_no_report_is_a_failed_axis(self):
        self.claude.finish("", axis="standards")

        code, output = self.run_bridge(self.claude_args(axis="standards"))

        self.assertEqual(code, 1)
        result = output["axes"]["standards"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["finalMessage"], "")
        self.assertTrue(result["reason"])

    def test_a_denied_reviewer_is_a_failed_axis_naming_the_denials(self):
        """A reviewer blocked from reading covered less than it was asked to."""
        self.claude.answer_with(
            {
                "session_id": "claude-standards",
                "result": "one finding",
                "is_error": False,
                "subtype": "success",
                "permission_denials": [{"tool_name": "Read"}],
            },
            axis="standards",
        )

        code, output = self.run_bridge(self.claude_args(axis="standards"))

        self.assertEqual(code, 1)
        result = output["axes"]["standards"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("1", result["reason"])
        self.assertIn("permission", result["reason"].lower())

    def test_output_that_is_not_the_one_json_object_is_a_failed_axis(self):
        self.claude.garble("standards")

        code, output = self.run_bridge(self.claude_args(axis="standards"))

        self.assertEqual(code, 1)
        result = output["axes"]["standards"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["finalMessage"], "")
        self.assertTrue(result["reason"])

    def test_a_half_open_run_keeps_the_completed_report_and_the_failed_reason(self):
        self.claude.finish("standards clear", axis="standards")
        self.claude.error("spec", "the spec reviewer went away")

        code, output = self.run_bridge(self.claude_args(axis="both"))

        self.assertEqual(code, 1)
        self.assertEqual(output["status"], "partially_completed")
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"], "standards clear"
        )
        self.assertNotIn("reason", output["axes"]["standards"])
        self.assertEqual(output["axes"]["spec"]["status"], "failed")
        self.assertEqual(output["axes"]["spec"]["finalMessage"], "")
        self.assertEqual(
            output["axes"]["spec"]["reason"], "the spec reviewer went away"
        )
        self.assertEqual(
            {state["axis"] for state in self.stored_sessions(expected_count=2)},
            {"standards", "spec"},
        )

    def test_an_axis_that_cannot_launch_leaves_no_reviewer_running(self):
        self.claude.finish("clear")
        self.claude.fail_launch("spec", "cannot launch headless Claude")

        with self.assertRaisesRegex(RuntimeError, "cannot launch"):
            self.run_bridge(self.claude_args(axis="both"))

        self.assertEqual(self.claude.alive, set())
        self.assertEqual(
            self.claude.killed, [process.pid for process in self.claude.launched]
        )
        self.assertFalse(list(self.state_dir.glob("*.json")))


class LaneDependencyTests(FakePaneTestCase):
    """Which Lane needs a terminal multiplexer and which does not."""

    def without_tmux(self):
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in ("TMUX", "TMUX_PANE")
        }
        self.enter(mock.patch.dict(os.environ, environment, clear=True))

    def test_the_claude_lane_reviews_with_no_tmux_at_all(self):
        self.without_tmux()
        self.claude.finish("no findings")

        code, output = self.run_bridge(self.args(reviewer="claude"))

        self.assertEqual(code, 0)
        self.assertEqual(output["axes"]["standards"]["finalMessage"], "no findings")

    def test_the_codex_lane_still_refuses_to_run_outside_tmux(self):
        self.without_tmux()
        self.codex.finish("no findings")

        with self.assertRaisesRegex(RuntimeError, "tmux"):
            self.run_bridge(self.args(reviewer="codex"))

        self.assertEqual(self.codex.launched_panes, [])

    def test_the_two_lanes_never_own_each_others_records(self):
        self.claude.finish("no findings")
        self.run_bridge(self.args(reviewer="claude"))
        claude_owner = self.stored_session()["owner"]

        codex_owner = self.bridge.resolve_owner(self.args()).to_dict()

        self.assertNotEqual(claude_owner, codex_owner)
        self.assertEqual(claude_owner["worktree_root"], str(self.worktree))


class SharedBriefTests(FakePaneTestCase):
    """One preparation, two Lanes, and the same brief delivered to both."""

    def deliver_through(self, reviewer):
        """Drive one review on one Lane, and hand back what it was told to review."""
        self.codex.finish("codex report")
        self.claude.finish("claude report")
        args = self.args(reviewer=reviewer, axis="both")

        code, output = self.run_bridge(args)

        self.assertEqual(code, 0)
        return args.preparation, output

    def test_one_preparation_delivers_the_same_brief_to_both_lanes(self):
        preparation, _output = self.deliver_through("codex")
        codex_briefs = [
            delivered_brief(turn["input"][0]["text"])
            for turn in self.codex.started_turns
        ]

        second_preparation, _second = self.deliver_through("claude")
        claude_briefs = [
            delivered_brief(prompt) for prompt in self.claude.prompts
        ]

        self.assertEqual(codex_briefs, claude_briefs)
        self.assertEqual(
            codex_briefs,
            [
                first_round_turn(self.bridge, preparation, "standards"),
                first_round_turn(self.bridge, preparation, "spec"),
            ],
        )
        self.assertEqual(
            [
                first_round_turn(self.bridge, second_preparation, "standards"),
                first_round_turn(self.bridge, second_preparation, "spec"),
            ],
            claude_briefs,
        )

    def test_both_lanes_report_the_same_result_shape(self):
        _preparation, codex_output = self.deliver_through("codex")
        _second, claude_output = self.deliver_through("claude")

        self.assertEqual(set(codex_output), set(claude_output))
        self.assertEqual(set(codex_output["axes"]), set(claude_output["axes"]))
        for axis in codex_output["axes"]:
            self.assertEqual(
                set(codex_output["axes"][axis]),
                set(claude_output["axes"][axis]),
            )
        self.assertEqual(
            codex_output["preparation"], claude_output["preparation"]
        )


class ClaudePerAxisChoiceTests(FakePaneTestCase):
    """Model and effort pinned per axis, on the Lane that takes them as flags."""

    def launched_choice(self, axis):
        """The model and effort one axis's reviewer was launched with."""
        command = next(
            process.command
            for process in self.claude.launched
            if process.axis == axis
        )
        return (
            command[command.index("--model") + 1] if "--model" in command else None,
            command[command.index("--effort") + 1] if "--effort" in command else None,
        )

    def test_the_spec_model_overrides_the_generic_one_for_only_the_spec_axis(self):
        self.claude.finish("standards clear", axis="standards")
        self.claude.finish("spec clear", axis="spec")

        code, _output = self.run_bridge(
            self.args(reviewer="claude", axis="both", model="m", effort="e",
                      spec_model="s2")
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.launched_choice("standards"), ("m", "e"))
        self.assertEqual(self.launched_choice("spec"), ("s2", "e"))
        states = {
            state["axis"]: (state["model"], state["effort"])
            for state in self.stored_sessions(expected_count=2)
        }
        self.assertEqual(states, {"standards": ("m", "e"), "spec": ("s2", "e")})

    def test_an_unpinned_axis_names_no_model_or_effort_at_all(self):
        self.claude.finish("standards clear", axis="standards")

        code, _output = self.run_bridge(
            self.args(reviewer="claude", axis="standards")
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.launched_choice("standards"), (None, None))

    def test_a_resumed_axis_keeps_its_own_saved_choices(self):
        self.claude.finish("round one", axis="spec")
        first_code, first_output = self.run_bridge(
            self.args(reviewer="claude", axis="spec", spec_model="spec-model",
                      spec_effort="high")
        )
        self.assertEqual(first_code, 0)
        session = first_output["axes"]["spec"]["reviewSessionId"]
        self.claude.finish("round two", axis="spec")

        code, output = self.run_bridge(
            self.args(reviewer="claude", axis="spec", resume_session=session)
        )

        self.assertEqual(code, 0)
        self.assertEqual(output["axes"]["spec"]["finalMessage"], "round two")
        self.assertEqual(output["axes"]["spec"]["reviewSessionId"], session)
        self.assertEqual(self.launched_choice("spec"), ("spec-model", "high"))
        state = self.bridge.SessionStore().read(session)
        self.assertEqual((state["model"], state["effort"]), ("spec-model", "high"))

    def test_a_resume_carries_the_lineage_the_first_round_started(self):
        """The lineage this Lane resumes is the one its first round started.

        Whether a resume is allowed at all is the Rounds Contract's answer, and
        `test_bridge_rounds.py` is where it is asserted.
        """
        self.claude.finish("round one", axis="spec")
        _code, first = self.run_bridge(
            self.args(reviewer="claude", axis="spec")
        )
        session = first["axes"]["spec"]["reviewSessionId"]
        self.claude.finish("round two", axis="spec")

        code, output = self.run_bridge(
            self.args(reviewer="claude", axis="spec", resume_session=session)
        )

        self.assertEqual(code, 0)
        self.assertEqual(output["axes"]["spec"]["finalMessage"], "round two")
        command = self.claude.launched[-1].command
        self.assertEqual(command[command.index("-r") + 1], "claude-spec")


class ClaudeRecoveryTests(FakePaneTestCase):
    """A killed driver leaves a headless reviewer running, and it is recovered."""

    def claude_args(self, **overrides):
        values = {"reviewer": "claude"}
        values.update(overrides)
        return self.args(**values)

    def kill_the_claude_driver(self, axis="standards"):
        self.claude.error(axis, DriverKilled())
        with self.assertRaises(DriverKilled):
            self.run_bridge(self.claude_args(axis=axis))
        del self.claude.axis_errors[axis]
        return self.stored_session()

    def test_a_killed_driver_leaves_a_recoverable_record_and_a_live_reviewer(self):
        state = self.kill_the_claude_driver()

        self.assertEqual(self.claude.alive, {self.claude.launched[0].pid})
        self.assertEqual(state["pid"], self.claude.launched[0].pid)
        self.assertTrue(pathlib.Path(state["runtimeDir"]).is_dir())

    def test_recovery_returns_the_same_handle_without_a_second_reviewer(self):
        killed = self.kill_the_claude_driver()
        self.claude.finish("two spec findings, one standards finding")
        launched_before = len(self.claude.launched)

        code, output = self.run_bridge(self.claude_args(recover_session=True))

        self.assertEqual(code, 0)
        result = output["axes"]["standards"]
        self.assertTrue(result["recovered"])
        self.assertEqual(result["reviewSessionId"], killed["reviewSessionId"])
        self.assertEqual(
            result["finalMessage"], "two spec findings, one standards finding"
        )
        self.assertEqual(
            len(self.claude.launched),
            launched_before,
            "recovery launched a second reviewer instead of waiting on the first",
        )

    def test_a_recovered_lineage_is_still_resumable_for_round_two(self):
        killed = self.kill_the_claude_driver(axis="spec")
        self.claude.finish("round one findings", axis="spec")
        self.run_bridge(self.claude_args(axis="spec", recover_session=True))
        self.claude.finish("round two findings", axis="spec")

        code, output = self.run_bridge(
            self.claude_args(
                axis="spec", resume_session=killed["reviewSessionId"]
            )
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            output["axes"]["spec"]["finalMessage"], "round two findings"
        )

    def test_a_driver_killed_the_moment_its_reviewer_was_up_leaves_it_findable(self):
        """The record is down before the waiting starts, not after.

        Patching out the waiting stands in for a driver killed in the instant
        between its reviewer starting and its first look at it.
        """
        killed_driver = self.enter(mock.patch.object(
            self.bridge.ClaudeLane, "drive", side_effect=DriverKilled
        ))
        with self.assertRaises(DriverKilled):
            self.run_bridge(self.claude_args())
        self.assertEqual(self.claude.alive, {self.claude.launched[0].pid})
        self.stop_patcher(killed_driver)
        self.claude.finish("the findings that reviewer went on to report")

        code, output = self.run_bridge(self.claude_args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"],
            "the findings that reviewer went on to report",
        )

    def test_a_report_already_read_survives_the_driver_that_read_it(self):
        """Nothing the reviewer printed is dropped until the caller has it."""
        self.claude.finish("the report the driver died holding")
        killed_driver = self.enter(mock.patch.object(
            self.bridge.ClaudeLane, "settle", side_effect=DriverKilled
        ))
        with self.assertRaises(DriverKilled):
            self.run_bridge(self.claude_args())
        self.stop_patcher(killed_driver)

        code, output = self.run_bridge(self.claude_args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"],
            "the report the driver died holding",
        )

    def test_giving_up_on_an_adopted_reviewer_stops_it(self):
        """An axis nobody can recover afterwards must leave nothing running."""
        killed = self.kill_the_claude_driver()

        code, output = self.run_bridge(
            self.claude_args(recover_session=True, timeout=0)
        )

        self.assertEqual(code, 1)
        self.assertEqual(output["axes"]["standards"]["status"], "failed")
        self.assertEqual(
            self.claude.killed, [self.claude.launched[0].pid]
        )
        self.assertFalse(
            pathlib.Path(killed["runtimeDir"]).exists(),
            "a settled axis still names a runtime directory to recover from",
        )

    def test_a_reviewer_that_never_finishes_is_stopped_rather_than_left_running(self):
        code, output = self.run_bridge(self.claude_args(timeout=0))

        self.assertEqual(code, 1)
        self.assertEqual(output["axes"]["standards"]["status"], "failed")
        self.assertEqual(self.claude.killed, [self.claude.launched[0].pid])
        self.assertEqual(self.claude.alive, set())

    def test_a_settled_review_leaves_nothing_to_recover(self):
        self.claude.finish("no findings")
        code, _output = self.run_bridge(self.claude_args())
        self.assertEqual(code, 0)

        with self.assertRaisesRegex(
            self.bridge.NoLiveSessionError, "No live review session"
        ):
            self.run_bridge(self.claude_args(recover_session=True))

    def test_another_worktree_recovers_nothing(self):
        self.kill_the_claude_driver()
        self.worktree_root = str(self.root / "another-worktree")

        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.claude_args(recover_session=True))

    def test_nothing_to_recover_exits_distinguishably_from_a_failed_review(self):
        with mock.patch.object(sys, "argv", [
            "review_bridge.py", "--reviewer", "claude",
            "--recover-session", "--cwd", str(self.worktree),
        ]):
            code = self.bridge.main()

        self.assertEqual(code, self.bridge.NO_LIVE_SESSION_EXIT)


CLAUDE_STUB = '''#!{python}
"""Stands in for the real headless claude executable."""

import json
import os
import pathlib
import sys

pathlib.Path({record!r}).write_text(
    json.dumps({{
        "argv": sys.argv[1:],
        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR", ""),
        "cwd": os.getcwd(),
    }}),
    encoding="utf-8",
)
print(json.dumps({{
    "session_id": "claude-stub-session",
    "result": "no findings",
    "is_error": False,
    "subtype": "success",
    "permission_denials": [],
}}))
'''


class ClaudeAccountTests(FakePaneTestCase):
    """Which login the reviewer spends on, exercised against a real process."""

    def setUp(self):
        super().setUp()
        self.use_real_claude_process()
        self.record = self.root / "reviewer-call.json"
        self.binary = self.root / "claude-stub"
        self.binary.write_text(
            CLAUDE_STUB.format(python=sys.executable, record=str(self.record)),
            encoding="utf-8",
        )
        self.binary.chmod(0o755)

    def review(self, **overrides):
        values = {
            "reviewer": "claude",
            "axis": "standards",
            "claude_binary": str(self.binary),
        }
        values.update(overrides)
        code, output = self.run_bridge(self.args(**values))
        self.assertEqual(code, 0, output)
        return json.loads(self.record.read_text(encoding="utf-8")), output

    def test_the_reviewer_runs_under_the_named_profile(self):
        profile = self.root / "profiles" / "second-account"
        profile.mkdir(parents=True)

        call, output = self.review(account=str(profile))

        self.assertEqual(call["config_dir"], str(profile))
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"], "no findings"
        )

    def test_without_an_account_the_environment_is_untouched(self):
        inherited = self.root / "profiles" / "the-callers-own"
        inherited.mkdir(parents=True)
        self.enter(
            mock.patch.dict(
                os.environ, {"CLAUDE_CONFIG_DIR": str(inherited)}, clear=False
            )
        )

        call, _output = self.review()

        self.assertEqual(call["config_dir"], str(inherited))

    def test_the_reviewer_reads_the_brief_in_the_reviewed_working_directory(self):
        call, _output = self.review()

        self.assertEqual(
            str(pathlib.Path(call["cwd"]).resolve()),
            str(self.worktree.resolve()),
        )
        self.assertEqual(call["argv"][0], "-p")
        self.assertIn("--output-format", call["argv"])


if __name__ == "__main__":
    unittest.main()
