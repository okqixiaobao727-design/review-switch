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

    def test_followup_starts_a_turn_on_the_saved_thread(self):
        client = FakeClient()
        state = {"threadId": "thread-ticket-50"}

        result = asyncio.run(
            self.bridge.start_followup_turn(client, state, "review again")
        )

        self.assertEqual(result["turn"]["id"], "turn-followup")
        self.assertEqual(
            client.requests,
            [
                (
                    "turn/start",
                    {
                        "threadId": "thread-ticket-50",
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

    def test_no_prompt_ever_reaches_the_tui_command_line(self):
        """A positional prompt is submitted at TUI startup, racing MCP (#14).

        The turn goes over the app-server once the readiness gate opens, so the
        launch command must carry nothing but the thread to attach to.
        """
        command = self.bridge.build_tui_command(
            base_args(),
            pathlib.Path("/tmp/app-server.sock"),
            "thread-ticket-50",
        )
        for argument in command:
            self.assertNotIn("Review HEAD", argument)
            self.assertNotIn("Rounds contract", argument)

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
            spec_source="docs/feature.md",
            spec_contents="Feature spec.",
            standards_files=("AGENTS.md",),
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

    def __init__(self, axis):
        self.axis = axis


class RecordingLane:
    """A Lane that records the briefs it is handed and answers with fixed results.

    Standing in for the codex Lane proves the harness reaches its reviewer only
    through the seam: nothing outside this class opens a pane or writes a record.
    """

    instances = []

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
        self.assertIn("\nSpec:\n", lane.briefs[1].text)

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
                "standardsFiles": ["AGENTS.md"],
                "codeGraphUsed": False,
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
        self.assertEqual(sum("\nSpec:\n" in prompt for prompt in prompts), 1)

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
                if "\nSpec:\n" in turn["input"][0]["text"]
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


if __name__ == "__main__":
    unittest.main()
