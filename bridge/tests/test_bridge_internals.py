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
    """A TUI-owned thread and its startup notifications/queue RPCs."""

    def __init__(
        self,
        inventory=("alpha", "beta"),
        loaded=("thread-tui",),
        disabled=(),
        failed=(),
        status_inventory=None,
    ):
        self.inventory = list(inventory)
        self.status_inventory = list(
            inventory if status_inventory is None else status_inventory
        )
        self.loaded = list(loaded)
        self.disabled = set(disabled)
        self.failed = set(failed)
        self.requests = []
        self.brief_queued = False
        self.startup_by_thread = {
            thread_id: {
                name: {
                    "threadId": thread_id,
                    "name": name,
                    "status": "failed" if name in self.failed else "ready",
                }
                for name in self.status_inventory
                if name not in self.disabled
            }
            for thread_id in self.loaded
        }

    def write_startup_log(self, path):
        lines = [
            json.dumps(status)
            for statuses in self.startup_by_thread.values()
            for status in statuses.values()
        ]
        pathlib.Path(path).write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8"
        )

    async def request(self, method, params):
        self.requests.append((method, dict(params)))
        if method == "thread/loaded/list":
            return {"data": list(self.loaded), "nextCursor": None}
        if method == "config/read":
            return {
                "config": {
                    "mcp_servers": {
                        name: {"enabled": name not in self.disabled}
                        for name in self.inventory
                    }
                }
            }
        if method == "thread/queue/add":
            self.brief_queued = True
            return {"queuedSubmission": {"id": "queued-1"}}
        raise AssertionError(f"unexpected request: {method}")

    async def __aexit__(self, *_ignored):
        return None


class FakeStore:
    def __init__(self, client):
        self.client = client
        self.written = {}
        self.reports = {}
        self.brief_queued_at_first_write = None

    def write(self, session_id, state):
        if self.brief_queued_at_first_write is None:
            self.brief_queued_at_first_write = self.client.brief_queued
        self.written[session_id] = state

    def write_report(self, session_id, final_message):
        """These tests pin the startup order, not what a settled report leaves."""
        self.reports[session_id] = final_message
        return f"/reports/{session_id}.md" if final_message.strip() else None


class CodexDeliveryStartupTests(unittest.TestCase):
    """A TUI owns the thread before the Bridge checks MCP and queues the Brief."""

    def setUp(self):
        self.bridge = load_bridge()
        self.owner = self.bridge.InvocationOwner(
            tmux_server="/tmp/tmux-501/default,1",
            origin_pane="%1",
            worktree_root="/workspace/ticket-50",
        )
        self.runtime_dirs = []
        self.cleaned = []

    def deliver_one_axis(self, client, startup_timeout=5):
        """Drive one axis through the codex Lane with everything but the gate faked out."""

        def fake_launch_pane(args, runtime_dir):
            self.runtime_dirs.append(pathlib.Path(runtime_dir))
            client.write_startup_log(
                pathlib.Path(runtime_dir) / self.bridge.MCP_STARTUP_LOG_FILENAME
            )
            return "%9"

        async def fake_connect(*_args, **_kwargs):
            return client

        async def fake_wait_for_review(*_args, **_kwargs):
            return {"id": "thread-new"}, {"id": "turn-1", "status": "completed"}

        args = base_args(cwd=os.getcwd(), tmux_target="%1",
                         startup_timeout=startup_timeout)
        self.store = FakeStore(client)
        with mock.patch.multiple(
            self.bridge,
            launch_pane=fake_launch_pane,
            connect_when_ready=fake_connect,
            wait_for_review=fake_wait_for_review,
            pane_exists=lambda pane_id: True,
            cleanup_pane=lambda pane, runtime: self.cleaned.append(pane),
        ):
            lane = self.bridge.CodexLane(args, self.owner, self.store)
            brief = self.bridge.axis_brief(args, args.axis)
            return asyncio.run(lane.deliver(lane.open(brief)))

    def test_the_tui_thread_and_its_mcp_notifications_precede_the_axis_brief(self):
        client = GateClient()

        self.deliver_one_axis(client)

        self.assertEqual(
            [method for method, _params in client.requests],
            [
                "thread/loaded/list",
                "config/read",
                "thread/queue/add",
            ],
        )
        self.assertNotIn(
            "mcpServerStatus/list",
            [method for method, _params in client.requests],
        )

    def test_the_bridge_neither_starts_nor_resumes_the_tui_owned_thread(self):
        client = GateClient()

        self.deliver_one_axis(client)

        methods = [method for method, _params in client.requests]
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("thread/resume", methods)
        self.assertNotIn("turn/start", methods)

    def test_the_record_is_on_disk_before_the_axis_brief_is_queued(self):
        client = GateClient()

        self.deliver_one_axis(client)

        self.assertFalse(
            self.store.brief_queued_at_first_write,
            "the Brief was queued before recovery could discover its record",
        )

    def test_missing_startup_notifications_do_not_hang_the_gate(self):
        client = GateClient(status_inventory=())

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "MCP startup"):
            with tempfile.TemporaryDirectory() as temp_dir:
                startup_log = pathlib.Path(temp_dir) / "mcp-startup.jsonl"
                client.write_startup_log(startup_log)
                asyncio.run(
                    self.bridge.wait_for_mcp_startup(
                        client,
                        "thread-tui",
                        os.getcwd(),
                        None,
                        0.3,
                        startup_log,
                    )
                )

        self.assertLess(time.monotonic() - started, 30)

    def test_an_unsettled_server_tears_the_pane_down_without_queueing(self):
        client = GateClient(status_inventory=())

        failure = self.deliver_one_axis(client, startup_timeout=0.2)

        self.assertIsInstance(failure, self.bridge.AxisFailure)
        self.assertIn("MCP startup", failure.reason)
        self.assertEqual(self.cleaned, ["%9"])
        self.assertFalse(client.brief_queued)

    def test_a_codex_without_mcp_servers_is_affirmatively_ready(self):
        client = GateClient(inventory=())
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_log = pathlib.Path(temp_dir) / "mcp-startup.jsonl"
            client.write_startup_log(startup_log)
            settled = asyncio.run(
                self.bridge.wait_for_mcp_startup(
                    client,
                    "thread-tui",
                    os.getcwd(),
                    None,
                    0.2,
                    startup_log,
                )
            )

        self.assertEqual(settled, {})

    def test_a_terminally_unavailable_optional_server_is_settled(self):
        client = GateClient(failed=("beta",))
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_log = pathlib.Path(temp_dir) / "mcp-startup.jsonl"
            client.write_startup_log(startup_log)
            settled = asyncio.run(
                self.bridge.wait_for_mcp_startup(
                    client,
                    "thread-tui",
                    os.getcwd(),
                    None,
                    0.2,
                    startup_log,
                )
            )

        self.assertEqual(settled["beta"]["status"], "failed")

    def test_an_enabled_server_missing_from_status_keeps_the_gate_closed(self):
        client = GateClient(status_inventory=("alpha",))

        with tempfile.TemporaryDirectory() as temp_dir:
            startup_log = pathlib.Path(temp_dir) / "mcp-startup.jsonl"
            client.write_startup_log(startup_log)
            with self.assertRaisesRegex(RuntimeError, "unsettled: beta"):
                asyncio.run(
                    self.bridge.wait_for_mcp_startup(
                        client,
                        "thread-tui",
                        os.getcwd(),
                        None,
                        0.2,
                        startup_log,
                    )
                )

    def test_a_disabled_server_does_not_block_readiness(self):
        client = GateClient(disabled=("beta",), status_inventory=("alpha",))
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_log = pathlib.Path(temp_dir) / "mcp-startup.jsonl"
            client.write_startup_log(startup_log)
            settled = asyncio.run(
                self.bridge.wait_for_mcp_startup(
                    client,
                    "thread-tui",
                    os.getcwd(),
                    None,
                    0.2,
                    startup_log,
                )
            )

        self.assertEqual(set(settled), {"alpha"})

    def test_a_pane_that_dies_before_status_returns_is_reported(self):
        client = GateClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            startup_log = pathlib.Path(temp_dir) / "mcp-startup.jsonl"
            client.write_startup_log(startup_log)
            with mock.patch.object(self.bridge, "pane_exists", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "pane exited before MCP"):
                    asyncio.run(
                        self.bridge.wait_for_mcp_startup(
                            client,
                            "thread-tui",
                            os.getcwd(),
                            "%9",
                            5,
                            startup_log,
                        )
                    )

    def test_proxy_log_records_only_thread_scoped_startup_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_log = pathlib.Path(temp_dir) / "mcp-startup.jsonl"
            self.bridge.record_mcp_startup_notification(
                startup_log,
                {
                    "method": "mcpServer/startupStatus/updated",
                    "params": {
                        "threadId": "thread-tui",
                        "name": "alpha",
                        "status": "starting",
                    },
                },
            )
            self.bridge.record_mcp_startup_notification(
                startup_log,
                {
                    "method": "mcpServer/startupStatus/updated",
                    "params": {
                        "threadId": "thread-tui",
                        "name": "alpha",
                        "status": "ready",
                    },
                },
            )
            self.bridge.record_mcp_startup_notification(
                startup_log,
                {
                    "method": "mcpServer/startupStatus/updated",
                    "params": {
                        "threadId": None,
                        "name": "global",
                        "status": "ready",
                    },
                },
            )

            statuses = self.bridge.read_mcp_startup_statuses(
                startup_log, "thread-tui"
            )

        self.assertEqual(statuses["alpha"]["status"], "ready")
        self.assertNotIn("global", statuses)

    def test_tui_proxy_forwards_both_directions_and_records_server_updates(self):
        class Source:
            def __init__(self, messages):
                self.messages = messages

            async def __aiter__(self):
                for message in self.messages:
                    yield message

        class Destination:
            def __init__(self):
                self.text = []

            async def send_str(self, value):
                self.text.append(value)

        message_type = self.bridge.aiohttp.WSMsgType.TEXT
        client_request = json.dumps({"id": 1, "method": "test", "params": {}})
        server_update = json.dumps(
            {
                "method": "mcpServer/startupStatus/updated",
                "params": {
                    "threadId": "thread-tui",
                    "name": "alpha",
                    "status": "ready",
                },
            }
        )
        client_source = Source([type("Message", (), {"type": message_type, "data": client_request})()])
        server_source = Source([type("Message", (), {"type": message_type, "data": server_update})()])
        upstream = Destination()
        downstream = Destination()

        with tempfile.TemporaryDirectory() as temp_dir:
            startup_log = pathlib.Path(temp_dir) / "startup.jsonl"
            asyncio.run(self.bridge.forward_websocket(client_source, upstream))
            asyncio.run(
                self.bridge.forward_websocket(
                    server_source, downstream, startup_log
                )
            )
            statuses = self.bridge.read_mcp_startup_statuses(
                startup_log, "thread-tui"
            )

        self.assertEqual(upstream.text, [client_request])
        self.assertEqual(downstream.text, [server_update])
        self.assertEqual(statuses["alpha"]["status"], "ready")

    def test_tui_proxy_accepts_the_codex_rpc_upgrade_path(self):
        app = self.bridge.build_tui_proxy_app(object())
        paths = {route.resource.canonical for route in app.router.routes()}

        self.assertEqual(paths, {"/rpc"})

    def test_a_fresh_app_server_rejects_more_than_one_tui_thread(self):
        client = GateClient(loaded=("thread-a", "thread-b"))

        with self.assertRaisesRegex(RuntimeError, "more than one thread"):
            asyncio.run(
                self.bridge.wait_for_tui_thread(client, None, None, 0.2)
            )


class RecoveryDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()
        self.marker = "[claude-tui-review-bridge:recovery]"
        self.state = {
            "threadId": "thread-tui",
            "marker": self.marker,
        }

    def run_delivery(self, client):
        with mock.patch.object(self.bridge, "pane_exists", return_value=True):
            return asyncio.run(
                self.bridge.ensure_review_delivery(
                    client,
                    self.state,
                    "Review the recorded scope",
                    "%9",
                    1,
                )
            )

    def test_a_matching_durable_queue_entry_is_not_added_again(self):
        marker = self.marker

        class QueuedClient:
            def __init__(self):
                self.requests = []

            async def request(self, method, params):
                self.requests.append((method, params))
                if method == "thread/queue/list":
                    return {
                        "data": [{"clientUserMessageId": marker}],
                        "nextCursor": None,
                    }
                raise AssertionError(f"unexpected request: {method}")

        client = QueuedClient()

        self.assertEqual(self.run_delivery(client), "queued")
        self.assertEqual(
            [method for method, _params in client.requests],
            ["thread/queue/list"],
        )

    def test_a_matching_entry_on_a_later_queue_page_is_not_added_again(self):
        marker = self.marker

        class PaginatedClient:
            def __init__(self):
                self.requests = []

            async def request(self, method, params):
                self.requests.append((method, params))
                if method != "thread/queue/list":
                    raise AssertionError(f"unexpected request: {method}")
                if params.get("cursor") is None:
                    return {
                        "data": [{"clientUserMessageId": "another-review"}],
                        "nextCursor": "page-two",
                    }
                return {
                    "data": [{"clientUserMessageId": marker}],
                    "nextCursor": None,
                }

        client = PaginatedClient()

        self.assertEqual(self.run_delivery(client), "queued")
        self.assertEqual(
            client.requests,
            [
                (
                    "thread/queue/list",
                    {"threadId": "thread-tui"},
                ),
                (
                    "thread/queue/list",
                    {"threadId": "thread-tui", "cursor": "page-two"},
                ),
            ],
        )

    def test_an_active_turn_without_its_user_item_is_waited_for(self):
        marker = self.marker

        class MaterializingClient:
            def __init__(self):
                self.read_count = 0
                self.requests = []

            async def request(self, method, params):
                self.requests.append((method, params))
                if method == "thread/queue/list":
                    return {"data": [], "nextCursor": None}
                if method == "thread/read":
                    self.read_count += 1
                    items = []
                    if self.read_count > 1:
                        items = [
                            {
                                "type": "userMessage",
                                "clientId": marker,
                                "content": [],
                            }
                        ]
                    return {
                        "thread": {
                            "status": {"type": "active"},
                            "turns": [
                                {
                                    "id": "turn-1",
                                    "status": "inProgress",
                                    "items": items,
                                }
                            ],
                        }
                    }
                raise AssertionError(f"unexpected request: {method}")

        client = MaterializingClient()

        self.assertEqual(self.run_delivery(client), "started")
        self.assertNotIn(
            "thread/queue/add",
            [method for method, _params in client.requests],
        )


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
