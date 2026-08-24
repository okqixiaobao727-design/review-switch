#!/usr/bin/env python3
"""The Bridge's kept internals: the readiness gate, the review turn, the rollout."""

import asyncio
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from bridge_harness import (
    RESOLVED_MODEL,
    ROUND_ONE_COUNTERS,
    ROUND_ONE_USAGE,
    ROUND_TWO_COUNTERS,
    ROUND_TWO_USAGE,
    base_args,
    load_bridge,
    token_count,
    turn_context,
    write_rollout,
)


class GateClient:
    """An app-server whose MCP servers announce themselves over successive pumps.

    `script` is one dict of name -> status per pump, merged in order, so a test
    can spell out the startup sequence the gate has to sit through.
    """

    def __init__(self, script, inventory=("alpha", "beta")):
        self.inventory = list(inventory)
        self.script = [dict(step) for step in script]
        self.mcp_startup = {}
        self.requests = []
        self.thread_start_params = None
        self.startup_when_turn_started = None

    async def request(self, method, params):
        self.requests.append(method)
        if method == "mcpServerStatus/list":
            return {"data": [{"name": name} for name in self.inventory]}
        if method == "thread/start":
            self.thread_start_params = dict(params)
            return {"thread": {"id": "thread-new"}}
        if method == "thread/resume":
            return {}
        if method == "turn/start":
            # The whole point of the gate: what MCP looked like at this moment.
            self.startup_when_turn_started = dict(self.mcp_startup)
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(f"unexpected request: {method}")

    async def pump(self, _seconds):
        if self.script:
            self.mcp_startup.update(self.script.pop(0))

    async def __aexit__(self, *_ignored):
        return None


class FakeStore:
    def __init__(self):
        self.written = {}
        # Whether the pane had already been handed its thread when the record
        # was written — the ordering a killed driver depends on.
        self.handoff_done_at_write = None

    def write(self, session_id, state):
        handoff = pathlib.Path(state["runtimeDir"]) / "thread-id"
        self.handoff_done_at_write = handoff.exists()
        self.written[session_id] = state


class McpReadinessGateTests(unittest.TestCase):
    """The first turn must not go in while a server is still coming up (#14)."""

    def setUp(self):
        self.bridge = load_bridge()
        self.owner = self.bridge.InvocationOwner(
            tmux_server="/tmp/tmux-501/default,1",
            origin_pane="%1",
            worktree_root="/workspace/ticket-50",
        )
        self.runtime_dirs = []
        self.cleaned = []

    def run_new_review(self, client, startup_timeout=5):
        """Drive run_new_review with everything but the gate faked out."""

        def fake_launch_pane(args, runtime_dir):
            self.runtime_dirs.append(pathlib.Path(runtime_dir))
            return "%9"

        async def fake_connect(*_args, **_kwargs):
            return client

        async def fake_wait_for_review(*_args, **_kwargs):
            return {"id": "thread-new"}, {"id": "turn-1", "status": "completed"}

        args = base_args(cwd=os.getcwd(), tmux_target="%1",
                         startup_timeout=startup_timeout)
        self.store = FakeStore()
        with mock.patch.multiple(
            self.bridge,
            launch_pane=fake_launch_pane,
            connect_when_ready=fake_connect,
            wait_for_review=fake_wait_for_review,
            pane_exists=lambda pane_id: True,
            cleanup_pane=lambda pane, runtime: self.cleaned.append(pane),
        ):
            return asyncio.run(
                self.bridge.run_new_review(args, self.owner, self.store)
            )

    def test_the_turn_waits_until_every_announced_server_has_settled(self):
        client = GateClient([
            {"alpha": "starting", "beta": "starting"},
            {"alpha": "ready"},
            {"beta": "ready"},
        ])

        self.run_new_review(client)

        self.assertEqual(
            client.startup_when_turn_started,
            {"alpha": "ready", "beta": "ready"},
            "the first turn was submitted while a server was still starting",
        )

    def test_the_thread_is_handed_over_only_after_the_turn_has_started(self):
        """`resume` refuses a thread with no rollout, so this order matters."""
        client = GateClient([{"alpha": "ready"}])

        self.run_new_review(client)

        handoff = self.runtime_dirs[0] / self.bridge.THREAD_HANDOFF_FILENAME
        self.assertEqual(handoff.read_text(encoding="utf-8"), "thread-new")
        self.assertIn("thread/start", client.requests)
        self.assertIn("turn/start", client.requests)

    def test_the_review_thread_carries_unattended_session_policy(self):
        client = GateClient([{"alpha": "ready"}])

        self.run_new_review(client)

        self.assertEqual(client.thread_start_params["approvalPolicy"], "never")
        self.assertEqual(client.thread_start_params["sandbox"], "danger-full-access")

    def test_the_record_is_on_disk_before_the_pane_is_handed_the_thread(self):
        """Otherwise a driver killed in between orphans a live, running review.

        The pane attaches as soon as it sees the thread id, so a record written
        after that leaves a window where a review is running that
        `--recover-session` cannot find.
        """
        client = GateClient([{"alpha": "ready"}])

        self.run_new_review(client)

        self.assertFalse(
            self.store.handoff_done_at_write,
            "the pane was handed its thread before the record was written",
        )

    def test_a_late_announcement_is_still_waited_for(self):
        """One server was measured announcing 169 ms after another went ready.

        `ghost` is configured but never announces, so the gate cannot simply
        wait for the whole inventory — and it must still not open in the window
        where alpha is ready and beta has not spoken yet.
        """
        client = GateClient(
            [
                {"alpha": "starting"},
                {"alpha": "ready"},
                {"beta": "starting"},
                {"beta": "ready"},
            ],
            inventory=("alpha", "beta", "ghost"),
        )

        settled = asyncio.run(self.bridge.wait_for_mcp_startup(client, None, 10))

        self.assertEqual(settled, {"alpha": "ready", "beta": "ready"})

    def test_an_inventory_call_that_hangs_does_not_hang_the_gate(self):
        """Nothing under `request` times out, so this RPC needs its own bound.

        Without one a stuck app-server holds the gate open past every budget the
        caller set, and only the pane's reaper eventually notices.
        """

        class HangingInventory:
            mcp_startup = {}

            async def request(self, method, _params):
                assert method == "mcpServerStatus/list"
                await asyncio.sleep(3600)

            async def pump(self, _seconds):
                raise AssertionError("the gate should never reach its poll loop")

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "which MCP servers are configured"):
            asyncio.run(
                self.bridge.wait_for_mcp_startup(HangingInventory(), None, 0.3)
            )

        self.assertLess(time.monotonic() - started, 30)

    def test_a_server_that_never_settles_is_named_in_the_failure(self):
        client = GateClient([{"alpha": "starting", "beta": "ready"}])

        with self.assertRaisesRegex(RuntimeError, "still starting: alpha"):
            asyncio.run(
                self.bridge.wait_for_mcp_startup(client, None, 0.2)
            )

        self.assertIsNone(
            client.startup_when_turn_started,
            "a turn was submitted even though the gate never opened",
        )

    def test_a_gate_that_times_out_tears_the_pane_down(self):
        client = GateClient([{"alpha": "starting"}])

        failure = self.run_new_review(client, startup_timeout=0.2)

        self.assertIsInstance(failure, self.bridge.AxisFailure)
        self.assertIn("Timed out waiting for Codex MCP", failure.reason)
        self.assertEqual(self.cleaned, ["%9"])
        self.assertIsNone(client.startup_when_turn_started)

    def test_silence_from_every_server_names_the_configured_ones(self):
        client = GateClient([])

        with self.assertRaisesRegex(RuntimeError, "none of alpha, beta announced"):
            asyncio.run(self.bridge.wait_for_mcp_startup(client, None, 0.2))

    def test_a_codex_without_mcp_servers_is_ready_at_once(self):
        client = GateClient([], inventory=())

        settled = asyncio.run(self.bridge.wait_for_mcp_startup(client, None, 0.2))

        self.assertEqual(settled, {})

    def test_a_pane_that_dies_during_startup_is_reported(self):
        client = GateClient([{"alpha": "starting"}])

        with mock.patch.object(self.bridge, "pane_exists", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "pane exited before its MCP"):
                asyncio.run(self.bridge.wait_for_mcp_startup(client, "%9", 5))


class WaitForReviewTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()
        self.marker = "[claude-tui-review-bridge:abc]"

    def thread_payload(self):
        return {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [{"type": "text", "text": self.marker}],
                            },
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "findings",
                            },
                        ],
                    }
                ],
            }
        }

    def test_a_thread_that_is_not_readable_yet_is_polled_again(self):
        """The rollout is not flushed the instant turn/start returns.

        Reading the thread fails until it is, which is this poll's normal first
        answer — treating it as fatal aborts a review that is running fine.
        """
        outer = self

        class FlakyClient:
            def __init__(self):
                self.reads = 0

            async def request(self, method, _params):
                assert method == "thread/read"
                self.reads += 1
                if self.reads == 1:
                    raise outer.bridge.AppServerError(
                        "thread/read failed: rollout at ... is empty"
                    )
                return outer.thread_payload()

        client = FlakyClient()
        with mock.patch.object(self.bridge, "pane_exists", return_value=True):
            thread, turn = asyncio.run(
                self.bridge.wait_for_review(client, "thread-1", self.marker, "%9", 10)
            )

        self.assertEqual(client.reads, 2)
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(thread["id"], "thread-1")

    def test_a_thread_that_never_becomes_readable_says_so(self):
        class DeadClient:
            async def request(self, _method, _params):
                raise outer_bridge.AppServerError("rollout is empty")

        outer_bridge = self.bridge
        with mock.patch.object(self.bridge, "pane_exists", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "never became readable"):
                asyncio.run(
                    self.bridge.wait_for_review(
                        DeadClient(), "thread-1", self.marker, "%9", 1
                    )
                )


class RolloutHarvestTests(unittest.TestCase):
    """Reading what a Codex review spent out of the rollout its thread id names.

    A pure read of a file the lane already wrote: no model token is spent to obtain it, and every
    way it can fail answers with the diagnosis the axis ends with instead.
    """

    def setUp(self):
        self.bridge = load_bridge()
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.sessions = pathlib.Path(self.work.name) / "sessions"

    def harvest(self, thread_id="thread-ticket-13"):
        return self.bridge.harvest_rollout(self.sessions, thread_id)

    def test_a_rollout_is_found_by_the_thread_id_its_filename_ends_in(self):
        write_rollout(
            self.sessions, "thread-ticket-13",
            [turn_context(RESOLVED_MODEL), token_count(ROUND_ONE_USAGE)],
        )
        write_rollout(
            self.sessions, "another-thread",
            [turn_context("gpt-5.6-luna"), token_count(ROUND_TWO_USAGE)],
        )

        counters, model, detail = self.harvest()

        self.assertIsNone(detail)
        self.assertEqual(counters, ROUND_ONE_COUNTERS)
        self.assertEqual(model, RESOLVED_MODEL)

    def test_the_counters_are_de_overlapped_so_they_sum_to_the_total_codex_reported(self):
        write_rollout(
            self.sessions, "thread-ticket-13",
            [turn_context(RESOLVED_MODEL), token_count(ROUND_ONE_USAGE)],
        )

        counters, _model, _detail = self.harvest()

        self.assertEqual(sum(counters.values()), ROUND_ONE_USAGE["total_tokens"])

    def test_a_resumed_round_is_read_as_one_review_from_the_last_cumulative_count(self):
        """Round two appends to the same rollout, and its counters already cover round one."""
        write_rollout(
            self.sessions, "thread-ticket-13",
            [
                turn_context(RESOLVED_MODEL),
                token_count(ROUND_ONE_USAGE),
                turn_context(RESOLVED_MODEL),
                token_count(ROUND_TWO_USAGE),
            ],
        )

        counters, _model, detail = self.harvest()

        self.assertIsNone(detail)
        self.assertEqual(counters, ROUND_TWO_COUNTERS)

    def test_a_rollout_that_counted_nothing_is_diagnosed_rather_than_billed_at_zero(self):
        path = write_rollout(
            self.sessions, "thread-ticket-13", [turn_context(RESOLVED_MODEL)]
        )

        counters, _model, detail = self.harvest()

        self.assertIsNone(counters)
        self.assertIn(str(path), detail)

    def test_a_thread_with_no_rollout_on_disk_is_diagnosed(self):
        write_rollout(self.sessions, "another-thread", [token_count(ROUND_ONE_USAGE)])

        counters, model, detail = self.harvest()

        self.assertIsNone(counters)
        self.assertIsNone(model)
        self.assertIn("thread-ticket-13", detail)
        self.assertIn(str(self.sessions), detail)

    def test_a_count_that_contradicts_itself_is_diagnosed_rather_than_invented(self):
        """More cached tokens than input tokens is not a figure to clamp into shape."""
        path = write_rollout(
            self.sessions, "thread-ticket-13",
            [
                turn_context(RESOLVED_MODEL),
                token_count({
                    "input_tokens": 100,
                    "cached_input_tokens": 400,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 50,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 150,
                }),
            ],
        )

        counters, _model, detail = self.harvest()

        self.assertIsNone(counters)
        self.assertIn(str(path), detail)

    def test_a_count_that_does_not_add_up_to_its_own_total_is_diagnosed(self):
        """The four mapped counters are the source's own total, or they are not its counters."""
        write_rollout(
            self.sessions, "thread-ticket-13",
            [
                turn_context(RESOLVED_MODEL),
                token_count(dict(ROUND_ONE_USAGE, total_tokens=209001)),
            ],
        )

        counters, _model, detail = self.harvest()

        self.assertIsNone(counters)
        self.assertIsNotNone(detail)

    def test_a_count_missing_one_of_its_figures_is_diagnosed_rather_than_read_as_zero(self):
        missing = dict(ROUND_ONE_USAGE)
        missing.pop("cache_write_input_tokens")
        write_rollout(
            self.sessions, "thread-ticket-13",
            [turn_context(RESOLVED_MODEL), token_count(missing)],
        )

        counters, _model, detail = self.harvest()

        self.assertIsNone(counters)
        self.assertIsNotNone(detail)

    def test_a_line_that_does_not_parse_mid_rollout_is_diagnosed(self):
        """A hole in the history is not a line to skip: what came after it is unaccounted for."""
        path = write_rollout(
            self.sessions, "thread-ticket-13",
            [turn_context(RESOLVED_MODEL), token_count(ROUND_ONE_USAGE)],
        )
        path.write_text(
            path.read_text(encoding="utf-8") + "{not json at all\n"
            + json.dumps(token_count(ROUND_TWO_USAGE)) + "\n",
            encoding="utf-8",
        )

        counters, _model, detail = self.harvest()

        self.assertIsNone(counters)
        self.assertIn(str(path), detail)

    def test_an_unreadable_last_count_is_diagnosed_rather_than_answered_by_an_older_one(self):
        """The last count is the thread's word on what it spent; an older one is not a stand-in."""
        path = write_rollout(
            self.sessions, "thread-ticket-13",
            [
                turn_context(RESOLVED_MODEL),
                token_count(ROUND_ONE_USAGE),
                {
                    "timestamp": "2026-08-18T04:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {}},
                },
            ],
        )

        counters, _model, detail = self.harvest()

        self.assertIsNone(counters)
        self.assertIn(str(path), detail)

    def test_a_half_written_last_line_does_not_lose_the_count_before_it(self):
        """The pane may still be writing, so a truncated tail is not a lost review."""
        path = write_rollout(
            self.sessions, "thread-ticket-13",
            [turn_context(RESOLVED_MODEL), token_count(ROUND_ONE_USAGE)],
        )
        with path.open("a", encoding="utf-8") as stream:
            stream.write('{"timestamp": "2026-08-18T04:0')

        counters, _model, detail = self.harvest()

        self.assertIsNone(detail)
        self.assertEqual(counters, ROUND_ONE_COUNTERS)


if __name__ == "__main__":
    unittest.main()
