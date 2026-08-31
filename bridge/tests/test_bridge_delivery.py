#!/usr/bin/env python3
"""Delivering a review: the panes it opens, the turns it starts, the records it leaves."""

import asyncio
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from bridge_harness import FakeClient, FakePaneTestCase, base_args, load_bridge


class DeliveryContractTests(unittest.TestCase):
    """The pane, the thread, and the record one Bridge call owns."""

    def setUp(self):
        self.bridge = load_bridge()

    def test_owner_uses_origin_pane_and_canonical_worktree(self):
        environment = {
            "TMUX": "/private/tmp/tmux-501/default,11028,2",
            "TMUX_PANE": "%24",
        }
        with mock.patch.object(
            self.bridge,
            "canonical_worktree_root",
            return_value="/workspace/ticket-50",
        ):
            owner = self.bridge.resolve_owner(base_args(), environment)

        self.assertEqual(owner.origin_pane, "%24")
        self.assertEqual(
            owner.tmux_server, "/private/tmp/tmux-501/default,11028"
        )
        self.assertEqual(owner.worktree_root, "/workspace/ticket-50")

    def test_parallel_panes_have_different_owner_keys(self):
        with mock.patch.object(
            self.bridge,
            "canonical_worktree_root",
            return_value="/workspace/ticket-51",
        ):
            owner_49 = self.bridge.resolve_owner(
                base_args(),
                {
                    "TMUX": "/private/tmp/tmux-501/default,11028,2",
                    "TMUX_PANE": "%23",
                },
            )
            owner_50 = self.bridge.resolve_owner(
                base_args(),
                {
                    "TMUX": "/private/tmp/tmux-501/default,11028,2",
                    "TMUX_PANE": "%24",
                },
            )

        self.assertNotEqual(owner_49.key, owner_50.key)

    def test_session_owner_rejects_another_parallel_pane(self):
        expected = self.bridge.InvocationOwner(
            tmux_server="/private/tmp/tmux-501/default,11028",
            origin_pane="%23",
            worktree_root="/workspace/ticket-49",
        )
        actual = self.bridge.InvocationOwner(
            tmux_server="/private/tmp/tmux-501/default,11028",
            origin_pane="%24",
            worktree_root="/workspace/ticket-50",
        )

        with self.assertRaisesRegex(RuntimeError, "belongs to another"):
            self.bridge.validate_session_owner(
                {"owner": expected.to_dict()}, actual
            )

    def test_launch_pane_requires_an_explicit_origin_target(self):
        with self.assertRaisesRegex(RuntimeError, "originating tmux pane"):
            self.bridge.launch_pane(
                base_args(tmux_target=None),
                pathlib.Path("/tmp/runtime"),
            )

    def test_axis_brief_is_added_to_the_saved_threads_durable_queue(self):
        client = FakeClient()

        result = asyncio.run(
            self.bridge.queue_review(
                client,
                "thread-ticket-50",
                "review again",
                "[claude-tui-review-bridge:brief-1]",
            )
        )

        self.assertEqual(result["queuedSubmission"]["id"], "queued-followup")
        self.assertEqual(
            client.requests,
            [
                (
                    "thread/queue/add",
                    {
                        "threadId": "thread-ticket-50",
                        "clientUserMessageId": (
                            "[claude-tui-review-bridge:brief-1]"
                        ),
                        "input": [
                            {
                                "type": "text",
                                "text": "review again",
                                "text_elements": [],
                            }
                        ],
                    },
                )
            ],
        )

    def test_the_tui_attaches_to_the_bridges_thread(self):
        command = self.bridge.build_tui_command(
            base_args(),
            pathlib.Path("/tmp/app-server.sock"),
            "thread-ticket-50",
        )
        self.assertEqual(command[-2:], ["resume", "thread-ticket-50"])

    def test_a_fresh_tui_starts_without_an_axis_brief_or_resume_target(self):
        """The TUI must own an idle thread before the Bridge delivers the Brief.

        A positional prompt would start a turn too early, while a resume target
        would require the not-yet-created thread that the TUI itself must own.
        """
        command = self.bridge.build_tui_command(
            base_args(),
            pathlib.Path("/tmp/app-server.sock"),
            None,
        )
        self.assertNotIn("resume", command)
        self.assertNotIn("Review HEAD", command)

    def test_pane_starts_the_observing_proxy_before_the_tui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = pathlib.Path(temp_dir)
            args = base_args(
                runtime_dir=temp_dir,
                cwd=temp_dir,
                startup_timeout=1,
                network=False,
                sandbox="danger-full-access",
                approval="never",
            )
            processes = [mock.Mock(name="app-server"), mock.Mock(name="proxy")]
            waits = []

            def ready(path, _timeout):
                waits.append(pathlib.Path(path).name)
                return True

            with mock.patch.object(
                self.bridge.subprocess, "Popen", side_effect=processes
            ) as popen, mock.patch.object(
                self.bridge.subprocess,
                "run",
                return_value=mock.Mock(returncode=0),
            ) as run, mock.patch.object(
                self.bridge, "wait_for_path", side_effect=ready
            ), mock.patch.object(
                self.bridge, "terminate_process"
            ), mock.patch.object(
                self.bridge.signal, "signal"
            ), mock.patch.object(
                self.bridge.shutil, "rmtree"
            ) as rmtree:
                code = self.bridge.run_pane(args)

        self.assertEqual(code, 0)
        self.assertEqual(waits, ["app-server.sock", "tui-proxy.sock"])
        self.assertIn("_tui_proxy", popen.call_args_list[1].args[0])
        tui_command = run.call_args.args[0]
        self.assertIn(f"unix://{runtime_dir / 'tui-proxy.sock'}", tui_command)
        rmtree.assert_not_called()

    def test_pane_runtime_waits_for_a_live_parent_to_collect_it(self):
        with mock.patch.object(
            self.bridge, "process_exists", return_value=True
        ), mock.patch.object(self.bridge.shutil, "rmtree") as rmtree:
            self.bridge.cleanup_orphaned_runtime(1234, "/tmp/runtime")

        rmtree.assert_not_called()

    def test_pane_runtime_self_reaps_after_its_parent_is_gone(self):
        with mock.patch.object(
            self.bridge, "process_exists", return_value=False
        ), mock.patch.object(self.bridge.shutil, "rmtree") as rmtree:
            self.bridge.cleanup_orphaned_runtime(1234, "/tmp/runtime")

        rmtree.assert_called_once_with("/tmp/runtime", ignore_errors=True)

    def test_session_store_is_overridable_and_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(
                os.environ,
                {"CODE_REVIEW_TUI_STATE_DIR": temp_dir},
                clear=False,
            ):
                store = self.bridge.SessionStore()
                store.write(
                    "session-ticket-50",
                    {
                        "version": self.bridge.SESSION_STATE_VERSION,
                        "reviewSessionId": "session-ticket-50",
                        "threadId": "thread-ticket-50",
                    },
                )
                state_path = pathlib.Path(temp_dir) / "session-ticket-50.json"
                self.assertEqual(
                    store.read("session-ticket-50")["threadId"],
                    "thread-ticket-50",
                )
                self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_session_id_cannot_escape_the_state_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.bridge.SessionStore(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "Invalid review session"):
                store.read("../another-task")
            # The report file is composed from the same id and guarded the same way.
            with self.assertRaisesRegex(RuntimeError, "Invalid review session"):
                store.write_report("../another-task", "a report")

    def test_same_pane_rejects_concurrent_bridge_calls(self):
        owner = self.bridge.InvocationOwner(
            tmux_server="/private/tmp/tmux-501/default,11028",
            origin_pane="%24",
            worktree_root="/workspace/ticket-50",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.bridge.SessionStore(temp_dir)
            with self.bridge.owner_lock(store, owner):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with self.bridge.owner_lock(store, owner):
                        pass

    def test_review_prompts_carry_the_axis_brief_and_no_rounds_contract(self):
        preparation = self.bridge.ReviewPreparation(
            scope=self.bridge.ReviewScope(
                fixed_point="main",
                resolved_fixed_point="abc123",
                fork_point="fed321",
            ),
            commit_list="def456 feature change",
            spec=self.bridge.SpecSlot(
                source="docs/feature.md",
                text="Spec: docs/feature.md. Read it before reviewing.",
                file="docs/feature.md",
            ),
            standards=self.bridge.StandardsSources(files=("AGENTS.md",)),
        )
        args = base_args(preparation=preparation, axis="standards")
        prompt = self.bridge.build_prompt(
            self.bridge.axis_brief(args, "standards"), "bridge-1"
        )

        for marker in (
            "$code-review",
            "/code-review",
            "mattpocock-skills",
            "Rounds contract",
            "one re-review",
            "coordinator",
        ):
            self.assertNotIn(marker, prompt)
        self.assertIn("Read-only review", prompt)
        self.assertIn("Diff: git diff fed321", prompt)
        self.assertIn(
            "New files not in that diff: "
            "git ls-files --others --exclude-standard",
            prompt,
        )


class DeliveredAxis:
    """What a stub Lane hands back for one axis."""

    thread_id = None
    #: This stub keeps no record, as an axis that failed before writing one has none.
    state = None

    def __init__(self, axis):
        self.axis = axis


class RecordingLane:
    """A Lane that records the briefs it is handed and answers with fixed results.

    Standing in for the codex Lane proves the harness reaches its reviewer only
    through the seam: nothing outside this class opens a pane or writes a record.
    """

    instances = []
    NEEDS_TMUX = True

    def __init__(self, args, owner, store):
        self.args = args
        self.owner = owner
        self.store = store
        self.briefs = []
        self.ended = []
        RecordingLane.instances.append(self)

    def open(self, brief):
        self.briefs.append(brief)
        return brief

    def discard(self, brief):
        raise AssertionError("an axis was discarded that had opened cleanly")

    async def deliver(self, brief):
        return DeliveredAxis(brief.axis)

    def settle(self, run):
        return {
            "status": "completed",
            "finalMessage": f"{run.axis} report",
            "reviewSessionId": f"session-{run.axis}",
        }

    def end_axis(self, axis, result, run):
        self.ended.append((axis, result["status"]))

    def mark_delivered(self, _run):
        """This recordless Lane has no persisted report to acknowledge."""


class DeliverySeamTests(FakePaneTestCase):
    """What the shared harness hands the Lane, and what it takes back."""

    def setUp(self):
        super().setUp()
        RecordingLane.instances = []

    def use_recording_lane(self):
        self.enter(
            mock.patch.dict(self.bridge.LANES, {"codex": RecordingLane}, clear=True)
        )

    def test_each_requested_axis_reaches_the_lane_as_one_brief(self):
        self.use_recording_lane()

        code, output = self.run_bridge(self.args(axis="both"))

        self.assertEqual(code, 0)
        lane = RecordingLane.instances[0]
        self.assertEqual([brief.axis for brief in lane.briefs], ["standards", "spec"])
        self.assertIn("Standards sources:", lane.briefs[0].text)
        self.assertIn("\nSpec: spec.md.", lane.briefs[1].text)

    def test_the_lane_result_is_the_result_the_run_reports(self):
        self.use_recording_lane()

        code, output = self.run_bridge(self.args(axis="both"))

        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"], "standards report"
        )
        self.assertEqual(output["axes"]["spec"]["finalMessage"], "spec report")
        self.assertEqual(
            RecordingLane.instances[0].ended,
            [("standards", "completed"), ("spec", "completed")],
        )

    def test_delivery_happens_only_inside_the_lane(self):
        self.use_recording_lane()

        self.run_bridge(self.args(axis="both"))

        self.assertEqual(self.codex.launched_panes, [])
        self.assertFalse(list(self.state_dir.glob("*.json")))

    def test_an_unknown_reviewer_fails_before_any_lane_opens(self):
        with self.assertRaises(RuntimeError) as raised:
            self.run_bridge(self.args(reviewer="gemini"))

        self.assertIn("gemini", str(raised.exception))
        self.assertIn("codex", str(raised.exception))
        self.assertEqual(self.codex.launched_panes, [])


class ReviewDeliveryTests(FakePaneTestCase):
    """A whole review driven through stubbed panes, and what it reports back."""

    def test_tui_attachment_does_not_interrupt_the_axis_brief_turn(self):
        self.codex.finish("visible review completed")

        code, output = self.run_bridge(self.args(axis="standards"))

        self.assertEqual(code, 0, output)
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"],
            "visible review completed",
        )
        self.assertEqual(
            self.codex.tui_attachments,
            [("standards", self.codex.thread_id)],
        )
        self.assertEqual(
            self.codex.control_events,
            [
                ("standards", "tui-attached"),
                ("standards", "mcp-ready"),
                ("standards", "brief-queued"),
            ],
        )
        self.assertEqual(len(self.codex.started_turns), 1)

    def test_a_standards_run_reports_what_was_prepared(self):
        fixed_point = self.fixed_point
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(base=fixed_point, spec="spec.md", axis="standards")
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.codex.panes, [])
        self.assertEqual(output["status"], "completed")
        self.assertEqual(set(output["axes"]), {"standards"})
        result = output["axes"]["standards"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finalMessage"], "no findings")
        self.assertNotIn("reason", result)
        state = self.stored_session()
        self.assertEqual(
            result["reviewSessionId"], state["reviewSessionId"]
        )
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["axis"], "standards")
        self.assertEqual(state["threadId"], self.codex.thread_id)
        self.assertEqual(state["paneId"], "%90")
        self.assertTrue(state["runtimeDir"])
        self.assertIsNone(state["model"])
        self.assertIsNone(state["effort"])
        self.assertEqual(
            output["preparation"],
            {
                "fixedPoint": fixed_point,
                "specSource": "spec.md",
                "specFile": "spec.md",
                "specFailure": None,
                "standardsFiles": ["AGENTS.md"],
                "standardsCondition": "absent",
                "codeGraphUsed": False,
                "responseFile": None,
            },
        )

    def test_both_axes_run_concurrently_in_two_panes_and_leave_two_records(self):
        self.codex.concurrent_turn_count = 2
        self.codex.finish("standards clear", axis="standards")
        self.codex.finish("spec clear", axis="spec")

        code, output = self.run_bridge(self.args(axis="both"))

        self.assertEqual(code, 0)
        self.assertEqual(self.codex.panes, [])
        self.assertEqual(self.codex.launched_panes, ["%90", "%91"])
        self.assertEqual(
            self.codex.launches,
            [
                ("standards", self.ORIGIN_PANE, "horizontal"),
                ("spec", "%90", "vertical"),
            ],
        )
        self.assertEqual(output["status"], "completed")
        self.assertEqual(set(output["axes"]), {"standards", "spec"})
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"], "standards clear"
        )
        self.assertEqual(output["axes"]["spec"]["finalMessage"], "spec clear")
        self.assertNotIn("reason", output["axes"]["standards"])
        self.assertNotIn("reason", output["axes"]["spec"])

        states = self.stored_sessions(expected_count=2)
        states_by_axis = {state["axis"]: state for state in states}
        self.assertEqual(set(states_by_axis), {"standards", "spec"})
        self.assertNotEqual(
            states_by_axis["standards"]["threadId"],
            states_by_axis["spec"]["threadId"],
        )
        for axis in ("standards", "spec"):
            self.assertEqual(
                output["axes"][axis]["reviewSessionId"],
                states_by_axis[axis]["reviewSessionId"],
            )

        prompts = [turn["input"][0]["text"] for turn in self.codex.started_turns]
        self.assertEqual(len(prompts), 2)
        self.assertEqual(sum("Standards sources:" in prompt for prompt in prompts), 1)
        self.assertEqual(sum("\nSpec: spec.md." in prompt for prompt in prompts), 1)

    def test_half_open_run_keeps_the_completed_report_and_failed_reason(self):
        self.codex.concurrent_turn_count = 2
        self.codex.finish("standards clear", axis="standards")
        self.codex.error("spec", "Timed out waiting for the spec review turn")

        code, output = self.run_bridge(self.args(axis="both"))

        self.assertEqual(code, 1)
        self.assertEqual(output["status"], "partially_completed")
        self.assertEqual(
            output["axes"]["standards"]["finalMessage"], "standards clear"
        )
        self.assertNotIn("reason", output["axes"]["standards"])
        self.assertEqual(output["axes"]["spec"]["status"], "failed")
        self.assertEqual(output["axes"]["spec"]["finalMessage"], "")
        self.assertTrue(output["axes"]["spec"]["reviewSessionId"])
        self.assertEqual(
            output["axes"]["spec"]["reason"],
            "Timed out waiting for the spec review turn",
        )
        self.assertEqual(self.codex.panes, [])
        self.assertEqual(
            {state["axis"] for state in self.stored_sessions(expected_count=2)},
            {"standards", "spec"},
        )

    def test_each_axis_report_is_written_where_a_human_can_open_it(self):
        """The result names a file, and the file holds that axis's report as markdown.

        The caller is given a path rather than the report a second time, so the
        path has to lead somewhere readable on its own.
        """
        self.codex.concurrent_turn_count = 2
        self.codex.finish("standards clear", axis="standards")
        self.codex.finish("spec clear", axis="spec")

        code, output = self.run_bridge(self.args(axis="both"))

        self.assertEqual(code, 0)
        paths = set()
        for axis in ("standards", "spec"):
            result = output["axes"][axis]
            report_file = pathlib.Path(result["reportFile"])
            self.assertEqual(report_file.suffix, ".md")
            self.assertEqual(
                report_file.read_text(encoding="utf-8"), result["finalMessage"]
            )
            paths.add(result["reportFile"])
        self.assertEqual(len(paths), 2)

    def test_an_axis_that_reported_nothing_names_no_report_file(self):
        """No report body, no file: an empty one would be a path to nothing."""
        self.codex.finish("")

        code, output = self.run_bridge(self.args(axis="standards"))

        self.assertEqual(code, 1)
        self.assertIsNone(output["axes"]["standards"]["reportFile"])
        self.assertEqual(list(self.state_dir.glob("*.md")), [])

    def test_an_axis_that_reported_only_whitespace_is_a_failed_axis(self):
        """Whitespace is not a report, and one rule says so everywhere.

        Were the file rule stricter than the status rule, an axis could complete
        and still have no report to point its caller at.
        """
        self.codex.finish("  \n\t ")

        code, output = self.run_bridge(self.args(axis="standards"))

        self.assertEqual(code, 1)
        result = output["axes"]["standards"]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["reason"])
        self.assertIsNone(result["reportFile"])
        self.assertEqual(list(self.state_dir.glob("*.md")), [])

    def test_completed_turn_without_a_report_is_a_failed_axis(self):
        self.codex.finish("")

        code, output = self.run_bridge(self.args(axis="standards"))

        self.assertEqual(code, 1)
        self.assertEqual(output["status"], "partially_completed")
        result = output["axes"]["standards"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["finalMessage"], "")
        self.assertTrue(result["reason"])
        self.assertTrue(result["reviewSessionId"])
        self.assertEqual(self.codex.panes, [])
        self.stored_session()

    def test_a_probe_skips_review_preparation(self):
        head = subprocess.run(
            ["git", "-C", str(self.worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.codex.finish("TUI_REVIEW_BRIDGE_OK")

        code, output = self.run_bridge(
            self.args(base=head, spec=None, axis="both", probe=True)
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"], None)
        self.assertEqual(self.codex.panes, [])
        self.assertEqual(self.codex.launched_panes, ["%90"])
        self.assertIn("This is a bridge health probe", prompt)
        # A probe reviews nothing, so it is asked for no verdict of its own.
        self.assertNotIn(self.bridge.FIRST_ROUND_VERDICT.request, prompt)


class PerAxisModelAndEffortTests(FakePaneTestCase):
    def test_generic_model_and_effort_apply_to_both_axes(self):
        self.codex.finish("standards clear", axis="standards")
        self.codex.finish("spec clear", axis="spec")

        code, _output = self.run_bridge(
            self.args(axis="both", model="m", effort="e")
        )

        self.assertEqual(code, 0)
        states = {
            state["axis"]: state
            for state in self.stored_sessions(expected_count=2)
        }
        self.assertEqual(
            {
                axis: (state["model"], state["effort"])
                for axis, state in states.items()
            },
            {"standards": ("m", "e"), "spec": ("m", "e")},
        )

    def test_spec_model_overrides_the_generic_model_for_only_the_spec_axis(self):
        self.codex.finish("standards clear", axis="standards")
        self.codex.finish("spec clear", axis="spec")

        code, _output = self.run_bridge(
            self.args(
                axis="both",
                model="m",
                effort="e",
                spec_model="s2",
            )
        )

        self.assertEqual(code, 0)
        states = {
            state["axis"]: state
            for state in self.stored_sessions(expected_count=2)
        }
        self.assertEqual(
            {
                axis: (state["model"], state["effort"])
                for axis, state in states.items()
            },
            {"standards": ("m", "e"), "spec": ("s2", "e")},
        )
        turn_choices = {
            (
                "spec"
                if "\nSpec: spec.md." in turn["input"][0]["text"]
                else "standards"
            ): (turn.get("model"), turn.get("effort"))
            for turn in self.codex.started_turns
        }
        self.assertEqual(
            turn_choices,
            {"standards": ("m", "e"), "spec": ("s2", "e")},
        )

    def test_standards_effort_alone_overrides_only_the_standards_axis(self):
        self.codex.finish("standards clear", axis="standards")
        self.codex.finish("spec clear", axis="spec")

        code, _output = self.run_bridge(
            self.args(axis="both", standards_effort="standards-effort")
        )

        self.assertEqual(code, 0)
        states = {
            state["axis"]: state
            for state in self.stored_sessions(expected_count=2)
        }
        self.assertEqual(
            {
                axis: (state["model"], state["effort"])
                for axis, state in states.items()
            },
            {
                "standards": (None, "standards-effort"),
                "spec": (None, None),
            },
        )

    def test_single_axis_run_ignores_the_other_axis_choices(self):
        self.codex.finish("spec clear", axis="spec")

        code, output = self.run_bridge(
            self.args(
                axis="spec",
                standards_model="ignored-model",
                standards_effort="ignored-effort",
            )
        )

        self.assertEqual(code, 0)
        self.assertEqual(set(output["axes"]), {"spec"})
        state = self.stored_session()
        self.assertEqual(state["axis"], "spec")
        self.assertIsNone(state["model"])
        self.assertIsNone(state["effort"])

    def test_followup_wakes_only_its_axis_in_a_fresh_pane_with_saved_choices(self):
        self.codex.finish("standards clear", axis="standards")
        self.codex.finish("spec needs a fix", axis="spec")
        first_code, _first_output = self.run_bridge(
            self.args(
                axis="both",
                spec_model="spec-model",
                spec_effort="high",
            )
        )
        self.assertEqual(first_code, 0)
        first_states = {
            state["axis"]: state
            for state in self.stored_sessions(expected_count=2)
        }
        standards_path = self.state_dir / (
            f"{first_states['standards']['reviewSessionId']}.json"
        )
        standards_record = standards_path.read_bytes()
        spec_state = first_states["spec"]
        panes_before = list(self.codex.launched_panes)
        self.codex.finish("spec fix is clear", axis="spec")

        code, output = self.run_bridge(
            self.args(
                axis="spec",
                resume_session=spec_state["reviewSessionId"],
            )
        )

        self.assertEqual(code, 0)
        self.assertEqual(set(output["axes"]), {"spec"})
        self.assertEqual(
            self.codex.launched_panes[len(panes_before):], ["%92"]
        )
        self.assertEqual(self.codex.launches[-1][0], "spec")
        self.assertEqual(self.codex.resumed_threads, [spec_state["threadId"]])
        self.assertEqual(self.codex.panes, [])
        saved_spec = self.bridge.SessionStore().read(
            spec_state["reviewSessionId"]
        )
        self.assertEqual(saved_spec["threadId"], spec_state["threadId"])
        self.assertEqual(saved_spec["model"], "spec-model")
        self.assertEqual(saved_spec["effort"], "high")
        self.assertEqual(
            (
                self.codex.started_turns[-1].get("model"),
                self.codex.started_turns[-1].get("effort"),
            ),
            ("spec-model", "high"),
        )
        self.assertEqual(standards_path.read_bytes(), standards_record)

    def test_a_followup_readiness_failure_closes_its_client_and_pane(self):
        self.codex.finish("round one", axis="spec")
        first_code, first_output = self.run_bridge(self.args(axis="spec"))
        self.assertEqual(first_code, 0)
        session_id = first_output["axes"]["spec"]["reviewSessionId"]
        closed_before = len(self.codex.closed_clients)
        self.codex.fail_mcp_startup("spec", "MCP inventory unavailable")

        code, output = self.run_bridge(
            self.args(axis="spec", resume_session=session_id)
        )

        self.assertEqual(code, 1)
        self.assertIn(
            "MCP inventory unavailable", output["axes"]["spec"]["reason"]
        )
        self.assertEqual(len(self.codex.closed_clients), closed_before + 1)
        self.assertEqual(self.codex.panes, [])

    def test_a_followup_connection_failure_keeps_the_original_reason(self):
        self.codex.finish("round one", axis="spec")
        first_code, first_output = self.run_bridge(self.args(axis="spec"))
        self.assertEqual(first_code, 0)
        session_id = first_output["axes"]["spec"]["reviewSessionId"]

        async def fail_connection(*_args, **_kwargs):
            raise RuntimeError("app-server connection failed")

        with mock.patch.object(
            self.bridge, "connect_when_ready", side_effect=fail_connection
        ):
            code, output = self.run_bridge(
                self.args(axis="spec", resume_session=session_id)
            )

        self.assertEqual(code, 1)
        self.assertIn(
            "app-server connection failed", output["axes"]["spec"]["reason"]
        )
        self.assertEqual(self.codex.panes, [])

    def test_a_timed_out_followup_settles_and_cleans_its_fresh_pane(self):
        self.codex.finish("round one", axis="spec")
        first_code, first_output = self.run_bridge(self.args(axis="spec"))
        self.assertEqual(first_code, 0)
        session_id = first_output["axes"]["spec"]["reviewSessionId"]
        self.codex.error("spec", "Timed out waiting for the spec review turn")

        code, output = self.run_bridge(
            self.args(axis="spec", resume_session=session_id)
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            output["axes"]["spec"]["reason"],
            "Timed out waiting for the spec review turn",
        )
        self.assertEqual(self.codex.panes, [])

    def test_followup_can_replace_its_axis_model_and_effort(self):
        self.codex.finish("round one clear", axis="spec")
        first_code, first_output = self.run_bridge(
            self.args(
                axis="spec",
                spec_model="spec-model-one",
                spec_effort="medium",
            )
        )
        self.assertEqual(first_code, 0)
        session_id = first_output["axes"]["spec"]["reviewSessionId"]
        self.codex.finish("round two clear", axis="spec")

        code, _output = self.run_bridge(
            self.args(
                axis="spec",
                resume_session=session_id,
                model="generic-model-two",
                effort="low",
                spec_model="spec-model-two",
                spec_effort="high",
            )
        )

        self.assertEqual(code, 0)
        state = self.bridge.SessionStore().read(session_id)
        self.assertEqual(state["axis"], "spec")
        self.assertEqual(state["model"], "spec-model-two")
        self.assertEqual(state["effort"], "high")
        self.assertEqual(
            (
                self.codex.started_turns[-1].get("model"),
                self.codex.started_turns[-1].get("effort"),
            ),
            ("spec-model-two", "high"),
        )


if __name__ == "__main__":
    unittest.main()
