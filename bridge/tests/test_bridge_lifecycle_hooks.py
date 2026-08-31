#!/usr/bin/env python3
"""The four Lifecycle Hook points, and the facts each firing carries."""

import collections
import contextlib
import io
import json
import os
import pathlib
import shlex
import sys
import unittest
from unittest import mock

from bridge_harness import (
    CLAUDE_ROUND_ONE_USAGE,
    FakePaneTestCase,
    RESOLVED_MODEL,
    ROUND_ONE_COUNTERS,
    ROUND_ONE_USAGE,
    ROUND_TWO_COUNTERS,
    ROUND_TWO_USAGE,
    token_count,
    turn_context,
    write_rollout,
)


HOOK_RECORDER = '''#!/usr/bin/env python3
"""Stands in for a caller's own hook command.

A Lifecycle Hook's whole contract is the command line the caller composed and
the facts the bridge puts in its environment, so this records exactly that: every
`REVIEW_` variable it was handed, plus the directory it was run in, one JSON line
per firing.
"""

import json
import os
import pathlib
import sys

record = {
    name: value for name, value in os.environ.items() if name.startswith("REVIEW_")
}
record["cwd"] = os.getcwd()
with pathlib.Path(sys.argv[1]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
'''


HOOK_POINT_FLAGS = (
    "--on-child-launch",
    "--on-review-start",
    "--on-axis-end",
    "--on-review-end",
)
# The four token counters an `axis-end` carries when its cost could be read.
COUNTER_VARS = {
    "input": "REVIEW_INPUT_TOKENS",
    "output": "REVIEW_OUTPUT_TOKENS",
    "cache_read": "REVIEW_CACHE_READ_TOKENS",
    "cache_creation": "REVIEW_CACHE_CREATION_TOKENS",
}


class HookRecordingTestCase(FakePaneTestCase):
    """A review driven from the command line with a real command at every point.

    The bridge learns no caller's vocabulary: a hook command is composed by the
    caller and carries whatever correlation token that caller needs, and the only
    thing the bridge adds is facts it owns. So a test here hands in a real command
    and pins the firings and the environment they arrive with.
    """

    MODEL = "gpt-5.6-luna"
    REVIEWER = "codex"

    def setUp(self):
        super().setUp()
        recorder = self.root / "record_hook.py"
        recorder.write_text(HOOK_RECORDER, encoding="utf-8")
        self.firings = self.root / "firings.jsonl"
        self.recorder_command = shlex.join(
            [sys.executable, str(recorder), str(self.firings)]
        )
        self.codex_home = self.root / "codex-home"
        os.environ["CODEX_HOME"] = str(self.codex_home)
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)

    def hooks(self, command=None):
        """The same command handed to every point; REVIEW_EVENT tells them apart."""
        command = self.recorder_command if command is None else command
        return tuple(
            argument for flag in HOOK_POINT_FLAGS for argument in (flag, command)
        )

    def main(self, *arguments):
        argv = [
            "review_bridge.py",
            "--reviewer", self.REVIEWER,
            "--cwd", str(self.worktree),
            *arguments,
        ]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(stdout):
                code = self.bridge.main()
        printed = stdout.getvalue().strip()
        self.output = json.loads(printed) if printed else None
        return code

    def review_argv(self, *arguments, timeout="5", axis="standards", hooks=None):
        # Every resume a caller makes carries a Response, so every resume this
        # file makes does too; a resume without one never reaches a hook point
        # at all, which is `test_bridge_response.py`'s to assert (#37).
        response = (
            ("--response", self.default_response_file())
            if "--resume-session" in arguments
            else ()
        )
        return (
            "--base", self.fixed_point,
            "--spec", "spec.md",
            "--axis", axis,
            "--model", self.MODEL,
            "--timeout", timeout,
            "--startup-timeout", "5",
            *(self.hooks() if hooks is None else hooks),
            *response,
            *arguments,
        )

    def firing_records(self):
        """Every hook firing, in the order the commands ran."""
        if not self.firings.exists():
            return []
        return [
            json.loads(line)
            for line in self.firings.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def events(self):
        return [record["REVIEW_EVENT"] for record in self.firing_records()]

    def firings_at(self, event):
        return [
            record for record in self.firing_records()
            if record["REVIEW_EVENT"] == event
        ]


class LifecycleHookTests(HookRecordingTestCase):
    """The four points a caller can hand a command for, and what each is told."""

    def write_rollout(self, thread_id, usage):
        return write_rollout(
            self.codex_home / "sessions",
            thread_id,
            [turn_context(RESOLVED_MODEL), token_count(usage)],
        )

    def test_a_single_axis_review_fires_each_point_once(self):
        self.codex.finish("one standards finding")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        # review-start comes before any Lane opens, so before the child launches.
        self.assertEqual(
            self.events(),
            ["review-start", "child-launch", "axis-end", "review-end"],
        )

    def test_a_two_axis_review_fires_once_per_axis_at_the_per_axis_points(self):
        self.codex.finish("no findings")

        code = self.main(*self.review_argv(axis="both"))

        self.assertEqual(code, 0)
        self.assertEqual(
            collections.Counter(self.events()),
            collections.Counter({
                "review-start": 1,
                "child-launch": 2,
                "axis-end": 2,
                "review-end": 1,
            }),
        )
        self.assertEqual(
            {record["REVIEW_AXIS"] for record in self.firings_at("axis-end")},
            {"standards", "spec"},
        )

    def test_a_resumed_review_fires_each_point_once(self):
        """Round one closed its pane, so round two opens one of its own.

        On the spec axis, which is the axis a lineage has a second round on.
        """
        self.codex.finish("round one findings", axis="spec")
        self.main(*self.review_argv(axis="spec"))
        session = self.stored_session()["reviewSessionId"]
        self.firings.unlink()

        code = self.main(
            *self.review_argv("--resume-session", session, axis="spec")
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            self.events(),
            ["review-start", "child-launch", "axis-end", "review-end"],
        )

    def test_a_resume_onto_a_live_pane_launches_no_further_child(self):
        """Nothing was launched, so nothing is announced as launched."""
        state = self.kill_the_driver(axis="spec")
        self.codex.finish("round two clear", axis="spec")

        code = self.main(
            *self.review_argv(
                "--resume-session", state["reviewSessionId"], axis="spec"
            )
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.events(), ["review-start", "axis-end", "review-end"])

    def test_a_review_that_fails_during_preparation_fires_only_the_review_end(self):
        code = self.main(
            "--base", "no-such-fixed-point",
            "--spec", "spec.md",
            "--timeout", "5",
            "--startup-timeout", "5",
            *self.hooks(),
        )

        self.assertEqual(code, 1)
        self.assertEqual(self.events(), ["review-end"])
        self.assertEqual(self.firings_at("review-end")[0]["REVIEW_STATUS"], "failed")

    def test_first_unfetched_document_fails_before_a_lane_opens(self):
        document = self.worktree / "docs/available.md"
        document.parent.mkdir(parents=True)
        document.write_text("Available.\n", encoding="utf-8")
        self.codex.finish("unexpected report", axis="requirements")
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = self.main(
                "--document", "docs/available.md",
                "--document", "docs/first-missing.md",
                "--document", "docs/second-missing.md",
                "--axis", "requirements",
                "--timeout", "5",
                "--startup-timeout", "5",
                *self.hooks(),
            )

        self.assertEqual(code, 1)
        self.assertEqual(
            stderr.getvalue().splitlines()[-1],
            "Document could not be fetched: docs/first-missing.md. "
            "Failure: spec file not found",
        )
        self.assertNotIn("second-missing", stderr.getvalue())
        self.assertEqual(self.codex.launched_panes, [])
        self.assertFalse(list(self.state_dir.glob("*.json")))
        self.assertEqual(self.events(), ["review-end"])
        review_end = self.firings_at("review-end")[0]
        self.assertEqual(review_end["REVIEW_STATUS"], "failed")
        self.assertEqual(
            pathlib.Path(review_end["cwd"]).resolve(), self.worktree.resolve()
        )

    def test_an_axis_that_cannot_be_briefed_fails_before_the_review_starts(self):
        """Preparation has not succeeded until every requested axis has a brief."""
        code = self.main(
            "--base", self.fixed_point,
            "--axis", "spec",
            "--timeout", "5",
            "--startup-timeout", "5",
            *self.hooks(),
        )

        self.assertEqual(code, 1)
        self.assertEqual(self.events(), ["review-end"])
        self.assertEqual(self.firings_at("review-end")[0]["REVIEW_STATUS"], "failed")

    def test_an_axis_already_launched_ends_when_a_later_pane_fails(self):
        launched = []

        def launch(args, runtime_dir):
            launched.append(args.axis)
            if len(launched) == 2:
                raise RuntimeError("tmux split-window failed")
            return self.codex.launch_pane(args, runtime_dir)

        self.enter(mock.patch.object(self.bridge, "launch_pane", launch))

        code = self.main(*self.review_argv(axis="both"))

        self.assertEqual(code, 1)
        self.assertEqual(
            self.events(),
            ["review-start", "child-launch", "axis-end", "review-end"],
        )
        record = self.firings_at("axis-end")[0]
        self.assertEqual(record["REVIEW_AXIS"], "standards")
        self.assertEqual(record["REVIEW_STATUS"], "failed")

    def test_a_resume_rejected_before_any_axis_started_ends_no_axis(self):
        self.codex.finish("round one findings")
        self.main(*self.review_argv())
        session = self.stored_session()["reviewSessionId"]
        self.firings.unlink()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = self.main(
                *self.review_argv("--resume-session", session, axis="spec")
            )

        self.assertEqual(code, 1)
        self.assertIn("axis 'standards'", stderr.getvalue())
        self.assertEqual(self.events(), ["review-end"])

    def test_a_caller_variable_of_its_own_reaches_the_hook_untouched(self):
        self.enter(mock.patch.dict(os.environ, {"REVIEW_COORDINATOR": "caller-42"}))
        self.codex.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        for record in self.firing_records():
            self.assertEqual(record["REVIEW_COORDINATOR"], "caller-42")

    def test_a_costed_axis_carries_no_inherited_reason_beside_its_counters(self):
        """The counters and the reason there are none are never both true."""
        self.enter(mock.patch.dict(
            os.environ, {"REVIEW_COST_DETAIL": "from an outer review"}
        ))
        self.write_rollout(self.codex.thread_id, ROUND_ONE_USAGE)
        self.codex.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        self.assertNotIn("REVIEW_COST_DETAIL", self.firings_at("axis-end")[0])

    def test_an_uncosted_axis_carries_no_inherited_counters_beside_its_reason(self):
        self.enter(mock.patch.dict(
            os.environ,
            {variable: "999" for variable in COUNTER_VARS.values()},
        ))
        self.codex.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        record = self.firings_at("axis-end")[0]
        self.assertIn("REVIEW_COST_DETAIL", record)
        for variable in COUNTER_VARS.values():
            self.assertNotIn(variable, record)

    def test_no_point_inherits_another_point_facts(self):
        """A stale fact from an outer review is not this review's to pass on."""
        self.enter(mock.patch.dict(
            os.environ,
            {
                "REVIEW_EVENT": "review-end",
                "REVIEW_AXIS": "spec",
                "REVIEW_SESSION": "an-outer-session",
            },
        ))
        self.codex.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        record = self.firings_at("review-end")[0]
        self.assertNotIn("REVIEW_AXIS", record)
        self.assertNotIn("REVIEW_SESSION", record)

    def test_a_recovery_with_nothing_to_recover_still_ends_its_review(self):
        code = self.main("--recover-session", "--timeout", "5", *self.hooks())

        self.assertEqual(code, self.bridge.NO_LIVE_SESSION_EXIT)
        self.assertEqual(self.events(), ["review-start", "review-end"])
        self.assertEqual(self.firings_at("review-end")[0]["REVIEW_STATUS"], "failed")

    def test_passing_no_hook_runs_nothing_extra(self):
        self.codex.finish("no findings")

        code = self.main(*self.review_argv(hooks=()))

        self.assertEqual(code, 0)
        self.assertFalse(self.firings.exists())

    def test_a_hook_that_fails_leaves_the_result_and_the_exit_status_alone(self):
        self.codex.finish("no findings")

        code = self.main(
            *self.review_argv(hooks=self.hooks(f"{self.recorder_command}; exit 3"))
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.output["status"], "completed")
        self.assertEqual(len(self.events()), 4)

    def test_a_hook_that_is_not_installed_leaves_the_review_standing(self):
        self.codex.finish("no findings")

        code = self.main(
            *self.review_argv(
                hooks=self.hooks("review-switch-hook-that-is-not-installed")
            )
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.output["status"], "completed")

    def test_a_hook_that_times_out_leaves_the_review_standing(self):
        self.enter(mock.patch.object(self.bridge, "HOOK_TIMEOUT_SECONDS", 0.2))
        self.codex.finish("no findings")

        code = self.main(
            *self.review_argv(hooks=self.hooks(f"{self.recorder_command}; sleep 30"))
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.output["status"], "completed")

    def test_every_hook_runs_in_the_reviewed_working_directory(self):
        self.codex.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        for record in self.firing_records():
            self.assertEqual(
                pathlib.Path(record["cwd"]).resolve(), self.worktree.resolve()
            )

    def test_the_child_launch_hook_names_the_child_and_its_pane(self):
        self.codex.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        record = self.firings_at("child-launch")[0]
        self.assertEqual(record["REVIEW_CHILD_CWD"], str(self.worktree))
        self.assertEqual(
            record["REVIEW_CHILD_TMUX_TARGET"], self.codex.launched_panes[0]
        )

    def test_the_review_start_hook_names_the_reviewer_the_model_and_the_axes(self):
        self.codex.finish("no findings")

        code = self.main(*self.review_argv(axis="both"))

        self.assertEqual(code, 0)
        record = self.firings_at("review-start")[0]
        self.assertEqual(record["REVIEW_REVIEWER"], "codex")
        self.assertEqual(record["REVIEW_MODEL"], self.MODEL)
        self.assertEqual(record["REVIEW_AXES"], "standards,spec")

    def test_a_single_axis_review_starts_naming_only_that_axis(self):
        self.codex.finish("no findings")

        code = self.main(*self.review_argv(axis="spec"))

        self.assertEqual(code, 0)
        self.assertEqual(self.firings_at("review-start")[0]["REVIEW_AXES"], "spec")

    def test_axes_pinned_to_different_models_start_with_no_single_model(self):
        self.codex.finish("no findings")

        code = self.main(
            *self.review_argv("--spec-model", "another-model", axis="both")
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.firings_at("review-start")[0]["REVIEW_MODEL"], "")

    def test_the_axis_end_hook_carries_the_axis_its_result_and_its_cost(self):
        self.write_rollout(self.codex.thread_id, ROUND_ONE_USAGE)
        self.codex.finish("one standards finding")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        record = self.firings_at("axis-end")[0]
        self.assertEqual(record["REVIEW_AXIS"], "standards")
        self.assertEqual(record["REVIEW_STATUS"], "completed")
        self.assertEqual(
            record["REVIEW_SESSION"], self.stored_session()["reviewSessionId"]
        )
        # The model the thread resolved to, not the alias the caller asked for.
        self.assertEqual(record["REVIEW_MODEL"], RESOLVED_MODEL)
        for counter, variable in COUNTER_VARS.items():
            self.assertEqual(record[variable], str(ROUND_ONE_COUNTERS[counter]))
        self.assertNotIn("REVIEW_COST_DETAIL", record)

    def test_the_axis_end_hook_names_the_file_holding_that_axis_report(self):
        """The hook is told where the report is, not handed the report."""
        self.codex.finish("one standards finding")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        record = self.firings_at("axis-end")[0]
        report_file = pathlib.Path(record["REVIEW_REPORT_FILE"])
        self.assertEqual(
            report_file.read_text(encoding="utf-8"), "one standards finding"
        )

    def test_an_axis_torn_down_before_it_opened_names_no_report_file(self):
        """A sibling that cannot open ends this axis before it has anything to report.

        No session, so no report — the rule `REVIEW_SESSION` already follows.
        """
        opened = self.bridge.CodexLane.open

        def refuse_the_spec_pane(lane, brief):
            if brief.axis == "spec":
                raise RuntimeError("no pane could be split for the spec axis")
            return opened(lane, brief)

        with mock.patch.object(
            self.bridge.CodexLane, "open", refuse_the_spec_pane
        ):
            code = self.main(*self.review_argv(axis="both"))

        self.assertEqual(code, 1)
        record = self.firings_at("axis-end")[0]
        self.assertEqual(record["REVIEW_AXIS"], "standards")
        self.assertEqual(record["REVIEW_SESSION"], "")
        self.assertEqual(record["REVIEW_REPORT_FILE"], "")

    def test_an_axis_whose_cost_cannot_be_read_says_why_instead(self):
        self.codex.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        record = self.firings_at("axis-end")[0]
        self.assertIn(self.codex.thread_id, record["REVIEW_COST_DETAIL"])
        for variable in COUNTER_VARS.values():
            self.assertNotIn(variable, record)

    def test_a_two_axis_review_costs_each_axis_from_its_own_thread(self):
        self.write_rollout("thread-standards-90", ROUND_ONE_USAGE)
        self.write_rollout("thread-spec-91", ROUND_TWO_USAGE)
        self.codex.finish("no findings")

        code = self.main(*self.review_argv(axis="both"))

        self.assertEqual(code, 0)
        costs = {
            record["REVIEW_AXIS"]: record["REVIEW_INPUT_TOKENS"]
            for record in self.firings_at("axis-end")
        }
        self.assertEqual(
            costs,
            {
                "standards": str(ROUND_ONE_COUNTERS["input"]),
                "spec": str(ROUND_TWO_COUNTERS["input"]),
            },
        )

    def test_an_axis_that_never_came_back_ends_as_a_failure_with_its_cost(self):
        self.write_rollout(self.codex.thread_id, ROUND_ONE_USAGE)

        code = self.main(*self.review_argv(timeout="0.2"))

        self.assertEqual(code, 1)
        record = self.firings_at("axis-end")[0]
        self.assertEqual(record["REVIEW_STATUS"], "failed")
        self.assertEqual(
            record["REVIEW_INPUT_TOKENS"], str(ROUND_ONE_COUNTERS["input"])
        )

    def test_an_axis_that_never_started_a_thread_still_ends_once(self):
        self.codex.fail_thread_start("standards", "thread could not start")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 1)
        self.assertEqual(
            [record["REVIEW_STATUS"] for record in self.firings_at("axis-end")],
            ["failed"],
        )

    def test_a_review_that_completed_ends_with_the_result_status(self):
        self.codex.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        self.assertEqual(
            self.firings_at("review-end")[0]["REVIEW_STATUS"], self.output["status"]
        )
        self.assertEqual(self.output["status"], "completed")

    def test_a_review_that_never_came_back_ends_with_the_result_status(self):
        code = self.main(*self.review_argv(timeout="0.2"))

        self.assertEqual(code, 1)
        self.assertEqual(
            self.firings_at("review-end")[0]["REVIEW_STATUS"], self.output["status"]
        )
        self.assertEqual(self.output["status"], "partially_completed")


class HeadlessAxisCostTests(HookRecordingTestCase):
    """What a headless axis is reported to have spent, at the same point.

    Cost is per axis whichever Lane drove it, and each Lane reads it where its
    own reviewer records it: a rollout on one, the result the reviewer printed on
    the other. What a caller's command is handed is the same either way.
    """

    REVIEWER = "claude"

    def test_a_costed_axis_carries_the_four_disjoint_counters(self):
        self.claude.bill(CLAUDE_ROUND_ONE_USAGE, model="claude-opus-5-billed")
        self.claude.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        record = self.firings_at("axis-end")[0]
        self.assertEqual(record["REVIEW_AXIS"], "standards")
        self.assertEqual(record["REVIEW_STATUS"], "completed")
        self.assertEqual(record["REVIEW_MODEL"], "claude-opus-5-billed")
        self.assertEqual(
            {
                name: int(record[variable])
                for name, variable in COUNTER_VARS.items()
            },
            ROUND_ONE_COUNTERS,
        )
        self.assertNotIn("REVIEW_COST_DETAIL", record)

    def test_an_axis_with_no_result_carries_the_reason_and_no_counters(self):
        self.claude.error("standards", "the reviewer went away")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 1)
        record = self.firings_at("axis-end")[0]
        self.assertEqual(record["REVIEW_STATUS"], "failed")
        self.assertTrue(record["REVIEW_COST_DETAIL"])
        for variable in COUNTER_VARS.values():
            self.assertNotIn(variable, record)

    def test_a_result_that_reported_no_usage_carries_the_reason(self):
        """A review that returned a report but no figures is not a costed axis."""
        self.claude.finish("no findings")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        record = self.firings_at("axis-end")[0]
        self.assertTrue(record["REVIEW_COST_DETAIL"])
        for variable in COUNTER_VARS.values():
            self.assertNotIn(variable, record)

    def test_a_headless_review_fires_each_point_once(self):
        self.claude.finish("one standards finding")

        code = self.main(*self.review_argv())

        self.assertEqual(code, 0)
        self.assertEqual(
            self.events(),
            ["review-start", "child-launch", "axis-end", "review-end"],
        )
        self.assertEqual(
            self.firings_at("child-launch")[0]["REVIEW_CHILD_TMUX_TARGET"], ""
        )


if __name__ == "__main__":
    unittest.main()
