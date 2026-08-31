#!/usr/bin/env python3
"""Recovering the review a killed driver left running."""

import asyncio
import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from bridge_harness import DriverKilled, FakePaneTestCase, load_bridge
from bridge_harness import (
    CLAUDE_ROUND_ONE_USAGE,
    RESOLVED_MODEL,
    ROUND_ONE_COUNTERS,
    ROUND_ONE_USAGE,
    token_count,
    turn_context,
    write_rollout,
)


class RecoveryRecordTests(unittest.TestCase):
    """Which records on disk a recovery is willing to reach for."""

    def setUp(self):
        self.bridge = load_bridge()

    def test_recovery_skips_records_from_state_version_one(self):
        owner = self.bridge.InvocationOwner(
            tmux_server="/tmp/tmux-501/default,1",
            origin_pane="%1",
            worktree_root="/workspace/ticket-50",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.bridge.SessionStore(temp_dir)
            store.write(
                "session-ticket-50",
                {
                    "version": 1,
                    "reviewSessionId": "session-ticket-50",
                    "owner": owner.to_dict(),
                    "threadId": "thread-ticket-50",
                    "marker": "[claude-tui-review-bridge:old]",
                },
            )

            self.assertEqual(self.bridge.SESSION_STATE_VERSION, 2)
            self.assertEqual(store.find_by_owner(owner), [])


class BufferedOutput(io.StringIO):
    """A pipe-like stream whose writes are invisible until explicitly flushed."""

    def __init__(self):
        super().__init__()
        self.visible = ""

    def flush(self):
        self.visible = self.getvalue()
        super().flush()


class RecoveryTests(FakePaneTestCase):
    """A driver killed mid-review is recovered, not restarted.

    The whole path runs through `run_bridge` against a stubbed pane: the first
    call is killed the way the harness kills it — the record is written, the
    pane lives on, nothing is printed — and the second call has only its own
    owner identity to work from.
    """

    def kill_the_two_axis_driver(self):
        """Kill a `both` call after both axis records and panes are live."""
        self.codex.error("spec", DriverKilled())
        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args(axis="both"))
        del self.codex.axis_errors["spec"]
        states = self.stored_sessions(expected_count=2)
        return {state["axis"]: state for state in states}

    def recording_axis_hook(self):
        record = self.root / "recovered-axis-hook.json"
        recorder = self.root / "record_recovered_axis_hook.py"
        recorder.write_text(
            "import json\n"
            "import os\n"
            "import pathlib\n"
            f"pathlib.Path({str(record)!r}).write_text(\n"
            "    json.dumps({\n"
            "        name: value for name, value in os.environ.items()\n"
            "        if name.startswith('REVIEW_')\n"
            "    }),\n"
            "    encoding='utf-8',\n"
            ")\n",
            encoding="utf-8",
        )
        return shlex.join([sys.executable, str(recorder)]), record

    def assert_stored_report_recovers_with_its_cost(
        self, state, reviewer, message, resolved_model
    ):
        report_file = self.state_dir / f"{state['reviewSessionId']}.md"
        self.assertEqual(
            state["report"],
            {
                "status": "completed",
                "finalMessage": message,
                "reviewSessionId": state["reviewSessionId"],
                "reportFile": str(report_file),
                "resolvedModel": resolved_model,
                "costCounters": ROUND_ONE_COUNTERS,
                "costDetail": None,
                "delivered": False,
            },
        )
        # The readable rendering outlives the driver the same way the record does.
        self.assertEqual(report_file.read_text(encoding="utf-8"), message)
        hook, hook_record = self.recording_axis_hook()

        code, output = self.run_bridge(self.args(
            reviewer=reviewer,
            recover_session=True,
            on_axis_end=hook,
        ))

        self.assertEqual(code, 0)
        result = output["axes"]["standards"]
        self.assertTrue(result["recovered"])
        self.assertEqual(result["finalMessage"], message)
        # A recovered delivery names the file the first delivery wrote, not a new one.
        self.assertEqual(result["reportFile"], str(report_file))
        self.assertEqual(output["preparation"], state["preparation"])
        hook_facts = json.loads(hook_record.read_text(encoding="utf-8"))
        self.assertEqual(hook_facts["REVIEW_REPORT_FILE"], str(report_file))
        self.assertEqual(hook_facts["REVIEW_MODEL"], resolved_model)
        self.assertEqual(
            {
                name: int(hook_facts[f"REVIEW_{name.upper()}_TOKENS"])
                for name in ROUND_ONE_COUNTERS
            },
            ROUND_ONE_COUNTERS,
        )
        self.assertTrue(self.stored_session()["report"]["delivered"])
        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.args(
                reviewer=reviewer,
                recover_session=True,
            ))

    def test_a_killed_driver_leaves_a_recoverable_record_and_a_live_pane(self):
        state = self.kill_the_driver()

        self.assertEqual(self.codex.panes, ["%90"])
        self.assertEqual(state["threadId"], self.codex.thread_id)
        self.assertEqual(state["marker"], self.codex.marker)
        self.assertEqual(state["owner"]["origin_pane"], self.ORIGIN_PANE)

    def test_recovery_delivers_a_recorded_but_not_yet_queued_brief_once(self):
        killed_driver = self.enter(mock.patch.object(
            self.bridge,
            "queue_review",
            side_effect=DriverKilled,
        ))

        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args())
        self.stop_patcher(killed_driver)

        recorded = self.stored_session()
        self.assertEqual(self.codex.panes, ["%90"])
        self.assertEqual(len(self.codex.tui_attachments), 1)
        self.assertEqual(self.codex.started_turns, [])
        self.codex.finish("the original handoff completed")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(
            output["axes"]["standards"]["reviewSessionId"],
            recorded["reviewSessionId"],
        )
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"],
            "the original handoff completed",
        )
        self.assertEqual(self.codex.launched_panes, ["%90"])
        self.assertEqual(len(self.codex.tui_attachments), 1)
        self.assertEqual(len(self.codex.started_turns), 1)

    def test_recovery_does_not_duplicate_a_queue_add_that_lost_its_response(self):
        self.codex.queue_add_exit_after_accept = DriverKilled()

        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args())

        recorded = self.stored_session()
        self.assertEqual(len(self.codex.started_turns), 1)
        self.codex.queue_add_exit_after_accept = None
        self.codex.finish("the accepted queued brief completed")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(
            output["axes"]["standards"]["reviewSessionId"],
            recorded["reviewSessionId"],
        )
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"],
            "the accepted queued brief completed",
        )
        self.assertEqual(
            len(self.codex.started_turns),
            1,
            "recovery queued the already accepted Brief a second time",
        )

    def test_recovery_returns_the_same_session_without_a_second_pane(self):
        killed = self.kill_the_driver()
        self.codex.finish("two spec findings, one standards finding")
        # The first review's own turn; recovery must not add to it.
        turns_before = len(self.codex.started_turns)

        code, output = self.run_bridge(
            self.args(recover_session=True)
        )

        self.assertEqual(code, 0)
        result = output["axes"]["standards"]
        self.assertEqual(result["reviewSessionId"], killed["reviewSessionId"])
        self.assertEqual(
            result["finalMessage"], "two spec findings, one standards finding"
        )
        self.assertEqual(self.codex.panes, [])
        self.assertEqual(
            len(self.codex.started_turns),
            turns_before,
            "recovery started a second turn instead of waiting on the first",
        )
        self.assertEqual(
            self.stored_session()["target"], killed["target"]
        )

    def test_recovery_returns_every_live_axis_with_its_original_handle(self):
        killed = self.kill_the_two_axis_driver()
        self.codex.finish("standards recovered", axis="standards")
        self.codex.finish("spec recovered", axis="spec")
        panes_before = list(self.codex.launched_panes)
        turns_before = len(self.codex.started_turns)

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(set(output["axes"]), {"standards", "spec"})
        for axis in ("standards", "spec"):
            result = output["axes"][axis]
            self.assertTrue(result["recovered"])
            self.assertEqual(
                result["reviewSessionId"], killed[axis]["reviewSessionId"]
            )
            self.assertEqual(result["finalMessage"], f"{axis} recovered")
        self.assertEqual(self.codex.launched_panes, panes_before)
        self.assertEqual(len(self.codex.started_turns), turns_before)
        self.assertEqual(self.codex.panes, [])

    def test_document_review_receipt_recovers_as_written_on_both_lanes(self):
        document = self.worktree / "docs/ticket.md"
        document.parent.mkdir(parents=True)
        document.write_text("Ticket.\n", encoding="utf-8")

        for reviewer in ("codex", "claude"):
            with self.subTest(reviewer=reviewer):
                lane = self.codex if reviewer == "codex" else self.claude
                lane_class = (
                    self.bridge.CodexLane
                    if reviewer == "codex"
                    else self.bridge.ClaudeLane
                )
                review_argv = [
                    "--reviewer", reviewer,
                    "--cwd", str(self.worktree),
                    "--document", "docs/ticket.md",
                    "--axis", "both",
                    "--no-network",
                ]
                lane.finish("delivered requirements", axis="requirements")
                lane.finish("delivered design", axis="design")

                delivered_code, delivered = self.run_bridge(
                    self.parsed_args(review_argv)
                )

                self.assertEqual(delivered_code, 0, delivered)
                self.assertEqual(
                    set(delivered["axes"]),
                    {"requirements", "design"},
                )

                lane.finish("recovered requirements", axis="requirements")
                lane.finish("recovered design", axis="design")
                killed_driver = self.enter(mock.patch.object(
                    lane_class,
                    "end_axis",
                    side_effect=DriverKilled,
                ))

                with self.assertRaises(DriverKilled):
                    self.run_bridge(self.parsed_args(review_argv))
                self.stop_patcher(killed_driver)

                recover_args = self.parsed_args([
                    "--reviewer", reviewer,
                    "--cwd", str(self.worktree),
                    "--recover-session",
                    "--no-network",
                ])
                code, output = self.run_bridge(recover_args)

                self.assertEqual(code, 0, output)
                self.assertEqual(
                    set(output["axes"]),
                    {"requirements", "design"},
                )
                for axis in ("requirements", "design"):
                    self.assertEqual(
                        output["axes"][axis]["finalMessage"],
                        f"recovered {axis}",
                    )
                    self.assertTrue(output["axes"][axis]["recovered"])
                self.assertEqual(
                    output["preparation"],
                    delivered["preparation"],
                )
                self.assertNotIn("specFile", output["preparation"])
                self.assertNotIn("specFailure", output["preparation"])

    def test_recovering_a_record_older_than_spec_files_still_names_one(self):
        """A review prepared before #33 was held to no spec file, and says so.

        The receipt is read back off the record rather than rebuilt, so a
        record written when the field did not exist would otherwise return a
        receipt missing it, and every caller reading the JSON would have to
        handle two shapes.
        """
        state = self.kill_the_driver()
        del state["preparation"]["specFile"]
        (self.state_dir / f"{state['reviewSessionId']}.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.codex.finish("legacy review findings")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertIn("specFile", output["preparation"])
        self.assertIsNone(output["preparation"]["specFile"])

    def test_recovering_a_record_older_than_spec_failures_names_none(self):
        """A legacy receipt has no failure detail for the Bridge to reconstruct."""
        state = self.kill_the_driver()
        del state["preparation"]["specFailure"]
        (self.state_dir / f"{state['reviewSessionId']}.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.codex.finish("legacy review findings")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertIn("specFailure", output["preparation"])
        self.assertIsNone(output["preparation"]["specFailure"])

    def test_recovery_preserves_the_recorded_spec_failure_exactly(self):
        self.codex.error("spec", DriverKilled())
        with self.assertRaises(DriverKilled):
            self.run_bridge(
                self.args(axis="spec", spec="docs/missing-spec.md")
            )
        del self.codex.axis_errors["spec"]
        state = self.stored_session()
        self.assertEqual(
            state["preparation"]["specFailure"], "spec file not found"
        )
        self.codex.finish("reviewed without the missing spec", axis="spec")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"], state["preparation"])
        self.assertEqual(
            output["preparation"]["specFailure"], "spec file not found"
        )

    def test_recovery_tolerates_a_record_with_no_preparation_report(self):
        state = self.kill_the_driver()
        del state["preparation"]
        (self.state_dir / f"{state['reviewSessionId']}.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.codex.finish("legacy review findings")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertIsNone(output["preparation"])

    def test_recovery_keeps_the_lineage_resumable_for_round_two(self):
        """On the spec axis, which is the axis a lineage has a round two on."""
        killed = self.kill_the_driver(axis="spec")
        self.codex.finish("round one findings", axis="spec")
        self.run_bridge(self.args(axis="spec", recover_session=True))
        turns_before = len(self.codex.started_turns)

        code, output = self.run_bridge(
            self.args(axis="spec", resume_session=killed["reviewSessionId"])
        )

        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(
            len(self.codex.started_turns) - turns_before,
            1,
            "round two should start exactly one follow-up turn",
        )
        self.assertEqual(self.codex.panes, [])

    def test_a_recovered_result_carries_the_call_its_original_invocation_recorded(self):
        caller_arguments = [
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            "--base", self.fixed_point,
            "--spec", "spec.md",
            "--no-network",
        ]
        self.codex.error("spec", DriverKilled())
        with self.assertRaises(DriverKilled):
            self.run_bridge(
                self.args(
                    axis="spec",
                    network=False,
                    caller_arguments=caller_arguments,
                )
            )
        del self.codex.axis_errors["spec"]
        state = self.stored_session()
        self.codex.finish("round one findings", axis="spec")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0, output)
        result = output["axes"]["spec"]
        response_file = str(
            self.state_dir / f"{state['reviewSessionId']}-response.md"
        )
        self.assertEqual(
            result["nextCall"]["argv"],
            [
                "review-bridge",
                *caller_arguments,
                "--axis", "spec",
                "--resume-session", state["reviewSessionId"],
                "--response", response_file,
            ],
        )
        self.assertEqual(result["nextCall"]["responseFile"], response_file)

    def test_a_record_from_before_next_calls_recovers_with_the_old_next_only(self):
        self.codex.error("spec", DriverKilled())
        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args(axis="spec"))
        state = self.stored_session()
        del state[self.bridge.NEXT_CALL_ARGUMENTS_FIELD]
        (self.state_dir / f"{state['reviewSessionId']}.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.codex.error("spec", "the recovered reviewer went away")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 1, output)
        result = output["axes"]["spec"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["next"], "fix then one re-review")
        self.assertIsNone(result["nextCall"])

    def test_a_recovered_axis_resumes_the_handle_it_was_recovered_under(self):
        """Recovery of a `both` call leaves each axis's own handle usable.

        Resumed on the spec axis, the axis a lineage has a round two on.
        """
        killed = self.kill_the_two_axis_driver()
        self.codex.finish("round one standards", axis="standards")
        self.codex.finish("round one spec", axis="spec")
        self.run_bridge(self.args(recover_session=True))
        turns_before = len(self.codex.started_turns)
        self.codex.finish("round two spec", axis="spec")

        code, output = self.run_bridge(
            self.args(
                axis="spec",
                resume_session=killed["spec"]["reviewSessionId"],
            )
        )

        self.assertEqual(code, 0)
        result = output["axes"]["spec"]
        self.assertEqual(
            result["reviewSessionId"], killed["spec"]["reviewSessionId"]
        )
        self.assertEqual(result["finalMessage"], "round two spec")
        self.assertEqual(len(self.codex.started_turns) - turns_before, 1)
        self.assertEqual(self.codex.panes, [])

    def test_another_origin_pane_recovers_nothing(self):
        self.kill_the_driver()
        os.environ["TMUX_PANE"] = "%777"

        with self.assertRaisesRegex(
            self.bridge.NoLiveSessionError, "No live review session"
        ):
            self.run_bridge(self.args(recover_session=True))

        self.assertEqual(self.codex.panes, ["%90"])

    def test_another_worktree_recovers_nothing(self):
        self.kill_the_driver()
        self.worktree_root = str(self.root / "another-worktree")

        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.args(recover_session=True))

    def test_a_dead_pane_recovers_nothing(self):
        self.kill_the_driver()
        self.codex.panes.clear()

        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.args(recover_session=True))

    def test_a_record_from_before_recovery_names_no_turn_to_wait_on(self):
        state = self.kill_the_driver()
        del state["marker"]
        (self.state_dir / f"{state['reviewSessionId']}.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.args(recover_session=True))

    def test_nothing_to_recover_exits_distinguishably_from_a_failed_review(self):
        with mock.patch.object(sys, "argv", [
            "review_bridge.py", "--reviewer", "codex",
            "--recover-session", "--cwd", str(self.worktree),
        ]):
            code = self.bridge.main()

        self.assertEqual(code, self.bridge.NO_LIVE_SESSION_EXIT)
        self.assertNotEqual(self.bridge.NO_LIVE_SESSION_EXIT, 1)
        self.assertEqual(self.codex.panes, [])

    def test_a_finished_codex_report_survives_its_dead_pane_until_delivery(self):
        codex_home = self.root / "codex-home"
        self.enter(mock.patch.dict(
            os.environ, {"CODEX_HOME": str(codex_home)}, clear=False
        ))
        write_rollout(
            codex_home / "sessions",
            self.codex.thread_id,
            [turn_context(RESOLVED_MODEL), token_count(ROUND_ONE_USAGE)],
        )
        self.codex.finish("the report the driver died holding")
        killed_driver = self.enter(mock.patch.object(
            self.bridge.CodexLane, "end_axis", side_effect=DriverKilled
        ))

        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args())
        self.stop_patcher(killed_driver)

        state = self.stored_session()
        self.assertEqual(self.codex.panes, [])
        self.assert_stored_report_recovers_with_its_cost(
            state,
            "codex",
            "the report the driver died holding",
            RESOLVED_MODEL,
        )

    def test_a_finished_claude_report_survives_its_deleted_result_until_delivery(self):
        self.claude.bill(
            CLAUDE_ROUND_ONE_USAGE,
            model="claude-opus-5-resolved",
        )
        self.claude.finish("the headless report the driver died holding")
        killed_driver = self.enter(mock.patch.object(
            self.bridge.ClaudeLane, "end_axis", side_effect=DriverKilled
        ))

        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args(reviewer="claude"))
        self.stop_patcher(killed_driver)

        state = self.stored_session()
        self.assertFalse(os.path.exists(state["runtimeDir"]))
        self.assert_stored_report_recovers_with_its_cost(
            state,
            "claude",
            "the headless report the driver died holding",
            "claude-opus-5-resolved",
        )

    def test_recovery_combines_a_stored_axis_with_its_still_live_sibling(self):
        self.codex.finish("standards stored", axis="standards")

        async def finish_one_axis_then_kill_the_driver(
            client, _thread_id, _marker, pane_id, _timeout
        ):
            if client.axis == "standards":
                return {"id": client.thread_id}, client.turn()
            while "%90" in self.codex.panes:
                await asyncio.sleep(0)
            raise DriverKilled()

        killed_driver = self.enter(mock.patch.object(
            self.bridge,
            "wait_for_review",
            side_effect=finish_one_axis_then_kill_the_driver,
        ))

        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args(axis="both"))
        self.stop_patcher(killed_driver)

        states = {
            state["axis"]: state
            for state in self.stored_sessions(expected_count=2)
        }
        self.assertIsNotNone(undelivered := states["standards"].get("report"))
        self.assertFalse(undelivered["delivered"])
        self.assertNotIn("report", states["spec"])
        self.assertEqual(self.codex.panes, ["%91"])
        self.codex.finish("spec recovered live", axis="spec")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(set(output["axes"]), {"standards", "spec"})
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"], "standards stored"
        )
        self.assertEqual(
            output["axes"]["spec"]["finalMessage"], "spec recovered live"
        )
        self.assertTrue(output["axes"]["standards"]["recovered"])
        self.assertTrue(output["axes"]["spec"]["recovered"])
        self.assertEqual(self.codex.panes, [])

    def assert_recovering_a_stored_re_review_spends_no_round(self, reviewer):
        lane = self.claude if reviewer == "claude" else self.codex
        lane_class = (
            self.bridge.ClaudeLane
            if reviewer == "claude"
            else self.bridge.CodexLane
        )
        lane.finish("round one findings", axis="spec")
        first_code, first_output = self.run_bridge(
            self.args(reviewer=reviewer, axis="spec")
        )
        self.assertEqual(first_code, 0)
        session = first_output["axes"]["spec"]["reviewSessionId"]
        lane.finish("round two findings", axis="spec")
        killed_driver = self.enter(mock.patch.object(
            lane_class, "end_axis", side_effect=DriverKilled
        ))

        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args(
                reviewer=reviewer,
                axis="spec",
                resume_session=session,
            ))
        self.stop_patcher(killed_driver)

        self.assertEqual(self.stored_session()["rounds"], 2)
        code, output = self.run_bridge(self.args(
            reviewer=reviewer,
            axis="spec",
            recover_session=True,
        ))

        self.assertEqual(code, 0)
        self.assertEqual(output["axes"]["spec"]["finalMessage"], "round two findings")
        self.assertEqual(output["axes"]["spec"]["next"], "escalate")
        self.assertEqual(self.stored_session()["rounds"], 2)

    def test_recovering_a_stored_codex_re_review_spends_no_round(self):
        self.assert_recovering_a_stored_re_review_spends_no_round("codex")

    def test_recovering_a_stored_claude_re_review_spends_no_round(self):
        self.assert_recovering_a_stored_re_review_spends_no_round("claude")

    def test_a_driver_killed_after_print_may_deliver_the_report_twice(self):
        self.codex.finish("the report printed before acknowledgement")
        killed_driver = self.enter(mock.patch.object(
            self.bridge.CodexLane,
            "mark_delivered",
            side_effect=DriverKilled,
        ))
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout), self.assertRaises(DriverKilled):
            asyncio.run(self.bridge.run_bridge(self.args()))
        self.stop_patcher(killed_driver)

        first_output = json.loads(stdout.getvalue())
        self.assertEqual(
            first_output["axes"]["standards"]["finalMessage"],
            "the report printed before acknowledgement",
        )
        self.assertFalse(self.stored_session()["report"]["delivered"])

        code, recovered = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(
            recovered["axes"]["standards"]["finalMessage"],
            "the report printed before acknowledgement",
        )

    def test_recovering_a_stored_codex_report_closes_its_surviving_pane(self):
        self.codex.finish("the report stored before cleanup")
        killed_driver = self.enter(mock.patch.object(
            self.bridge, "cleanup_pane", side_effect=DriverKilled
        ))

        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args())
        self.stop_patcher(killed_driver)

        self.assertFalse(self.stored_session()["report"]["delivered"])
        self.assertEqual(self.codex.panes, ["%90"])

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0)
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"],
            "the report stored before cleanup",
        )
        self.assertEqual(self.codex.panes, [])

    def test_output_is_visible_before_the_report_is_marked_delivered(self):
        self.codex.finish("the report flushed before acknowledgement")
        original_mark_delivered = self.bridge.CodexLane.mark_delivered

        def mark_then_kill(lane, run):
            original_mark_delivered(lane, run)
            raise DriverKilled()

        killed_driver = self.enter(mock.patch.object(
            self.bridge.CodexLane,
            "mark_delivered",
            autospec=True,
            side_effect=mark_then_kill,
        ))
        stdout = BufferedOutput()

        with contextlib.redirect_stdout(stdout), self.assertRaises(DriverKilled):
            asyncio.run(self.bridge.run_bridge(self.args()))
        self.stop_patcher(killed_driver)

        output = json.loads(stdout.visible)
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"],
            "the report flushed before acknowledgement",
        )
        self.assertTrue(self.stored_session()["report"]["delivered"])
        with self.assertRaises(self.bridge.NoLiveSessionError):
            self.run_bridge(self.args(recover_session=True))


if __name__ == "__main__":
    unittest.main()
