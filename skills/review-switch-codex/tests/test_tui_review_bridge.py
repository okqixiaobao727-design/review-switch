#!/usr/bin/env python3

import argparse
import asyncio
import collections
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BRIDGE_PATH = (
    pathlib.Path(__file__).parents[1] / "scripts" / "tui_review_bridge.py"
)


def load_bridge():
    spec = importlib.util.spec_from_file_location("tui_review_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StaticPreparation:
    def brief(self, axis):
        return f"Read-only {axis} review"

    def report(self):
        return {
            "fixedPoint": "resolved-main",
            "specSource": "docs/feature.md",
            "standardsFiles": [],
            "codeGraphUsed": False,
        }


def base_args(**overrides):
    values = {
        "base": "main",
        "spec": "docs/feature.md",
        "axis": "standards",
        "preparation": StaticPreparation(),
        "cwd": "/workspace/ticket-50",
        "timeout": 1,
        "startup_timeout": 1,
        "sandbox": "danger-full-access",
        "approval": "never",
        "network": False,
        "tmux_target": None,
        "resume_session": None,
        "recover_session": False,
        "model": None,
        "effort": None,
        "standards_model": None,
        "standards_effort": None,
        "spec_model": None,
        "spec_effort": None,
        "probe": False,
        "browser_probe": False,
        "resume_state": None,
        "status": "failed",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def initialize_review_repo(worktree):
    subprocess.run(
        [
            "git",
            "-c",
            "init.defaultBranch=main",
            "init",
            "--quiet",
            str(worktree),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "config",
            "user.email",
            "test@example.com",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "commit.gpgsign", "false"],
        check=True,
    )
    (worktree / "README.md").write_text("baseline\n", encoding="utf-8")
    (worktree / "AGENTS.md").write_text(
        "Follow the documented standards.\n", encoding="utf-8"
    )
    (worktree / "spec.md").write_text("Feature spec.\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "--quiet", "-m", "initial"],
        check=True,
    )
    fixed_point = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (worktree / "feature.py").write_text(
        "FEATURE_ENABLED = True\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(worktree), "add", "feature.py"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "commit",
            "--quiet",
            "-m",
            "feature change",
        ],
        check=True,
    )
    return fixed_point


class FakeSubprocess:
    """Stands in for the module's `subprocess` while a launch is exercised.

    Only the bridge's own calls are faked; the hook runs its command for real
    through its own module.
    """

    def __init__(self, stdout=""):
        self.stdout = stdout
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append(command)
        return argparse.Namespace(returncode=0, stdout=self.stdout, stderr="")


class FakeClient:
    def __init__(self):
        self.requests = []

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn-followup"}}
        raise AssertionError(f"unexpected request: {method}")


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


class BridgeContractTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def test_code_graph_result_is_trimmed_to_the_navigation_contract(self):
        graph_result = {
            "risk_score": 0.93,
            "test_gaps": [{"name": "late_change"}],
            "context_savings": {"estimated_tokens_saved": 1200},
            "changed_functions": [
                {
                    "file_path": "/workspace/project/src/later.py",
                    "line_start": 30,
                    "line_end": 35,
                    "name": "late_change",
                    "risk_score": 0.12,
                },
                {
                    "file_path": "/workspace/project/src/first.py",
                    "line_start": 5,
                    "line_end": 9,
                    "name": "read_first",
                    "risk_score": 0.93,
                },
            ],
            "review_priorities": [
                {
                    "file_path": "/workspace/project/src/first.py",
                    "line_start": 5,
                    "line_end": 9,
                    "name": "read_first",
                    "risk_score": 0.93,
                }
            ],
        }

        block = self.bridge.build_navigation_block(
            graph_result, "/workspace/project"
        )

        self.assertEqual(
            block,
            "src/first.py:5–9  read_first\n"
            "src/later.py:30–35  late_change",
        )
        for excluded in ("risk_score", "test_gaps", "context_savings"):
            self.assertNotIn(excluded, block)

    def test_navigation_keeps_every_changed_function_without_a_cap(self):
        changed_functions = [
            {
                "file_path": f"/workspace/project/src/change_{index}.py",
                "line_start": index + 1,
                "line_end": index + 1,
                "name": f"change_{index}",
            }
            for index in range(12)
        ]

        block = self.bridge.build_navigation_block(
            {
                "changed_functions": changed_functions,
                "review_priorities": [],
            },
            "/workspace/project",
        )

        self.assertEqual(len(block.splitlines()), len(changed_functions))
        self.assertEqual(
            block.splitlines()[-1],
            "src/change_11.py:12–12  change_11",
        )

    def test_parser_accepts_the_review_preparation_inputs(self):
        args = self.bridge.parse_args(
            [
                "--base",
                "main",
                "--spec",
                "docs/feature.md",
                "--axis",
                "standards",
            ]
        )

        self.assertEqual(args.base, "main")
        self.assertEqual(args.spec, "docs/feature.md")
        self.assertEqual(args.axis, "standards")

    def test_parser_defaults_to_both_axes(self):
        args = self.bridge.parse_args(
            ["--base", "main", "--spec", "docs/feature.md"]
        )

        self.assertEqual(args.axis, "both")

    def test_parser_accepts_optional_model_and_effort_for_each_axis(self):
        args = self.bridge.parse_args(
            [
                "--base",
                "main",
                "--spec",
                "docs/feature.md",
                "--standards-model",
                "standards-model",
                "--standards-effort",
                "standards-effort",
                "--spec-model",
                "spec-model",
                "--spec-effort",
                "spec-effort",
            ]
        )

        self.assertEqual(args.standards_model, "standards-model")
        self.assertEqual(args.standards_effort, "standards-effort")
        self.assertEqual(args.spec_model, "spec-model")
        self.assertEqual(args.spec_effort, "spec-effort")

    def test_per_axis_model_and_effort_default_to_none(self):
        args = self.bridge.parse_args(
            ["--base", "main", "--spec", "docs/feature.md"]
        )

        self.assertIsNone(args.standards_model)
        self.assertIsNone(args.standards_effort)
        self.assertIsNone(args.spec_model)
        self.assertIsNone(args.spec_effort)

    def test_parser_rejects_the_old_free_text_target(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(
                ["--base", "main", "--spec", "docs/feature.md", "review HEAD"]
            )

    def test_standards_axis_brief_matches_the_fixed_text(self):
        brief = self.bridge.build_standards_brief(
            "base-ref",
            "abc1234 feature one\ndef5678 feature two",
            ["AGENTS.md", "docs/agents/domain.md"],
        )

        self.assertEqual(
            brief,
            """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff base-ref...HEAD
Commits:
abc1234 feature one
def5678 feature two

Standards sources: AGENTS.md, docs/agents/domain.md

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

Report, per file/hunk where relevant, (a) every place the diff violates a documented standard: cite the standard (file + the rule); and (b) any baseline smell you spot: name it and quote the hunk. Distinguish hard violations from judgement calls: documented-standard breaches can be hard, but baseline smells are always judgement calls, and a documented repo standard overrides the baseline. Skip anything tooling enforces. Under 400 words.""",
        )

    def test_spec_axis_brief_matches_the_fixed_text(self):
        brief = self.bridge.build_spec_brief(
            "base-ref",
            "abc1234 feature one\ndef5678 feature two",
            "Feature title\n\nThe feature must keep the contract.",
        )

        self.assertEqual(
            brief,
            """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff base-ref...HEAD
Commits:
abc1234 feature one
def5678 feature two

Spec:
Feature title

The feature must keep the contract.

Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words.""",
        )

    def test_both_axis_briefs_receive_the_identical_navigation_block(self):
        preparation = self.bridge.ReviewPreparation(
            fixed_point="base-ref",
            resolved_fixed_point="abc123",
            commit_list="def456 feature change",
            spec_source="spec.md",
            spec_contents="Feature spec.",
            standards_files=("AGENTS.md",),
            navigation_block=(
                "src/first.py:5–9  read_first\n"
                "src/later.py:30–35  late_change"
            ),
        )
        expected_suffix = (
            "\n\nStart here (from the code graph; the diff is the full scope):\n"
            "src/first.py:5–9  read_first\n"
            "src/later.py:30–35  late_change"
        )

        self.assertTrue(preparation.brief("standards").endswith(expected_suffix))
        self.assertTrue(preparation.brief("spec").endswith(expected_suffix))

    def test_standards_brief_names_the_documented_fallback(self):
        brief = self.bridge.build_standards_brief(
            "main", "abc123 feature change", []
        )

        self.assertIn(
            "Standards sources: none documented; baseline only", brief
        )

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

    def test_parser_exposes_explicit_resume_handle(self):
        args = self.bridge.build_parser().parse_args(
            [
                "--base",
                "main",
                "--spec",
                "docs/feature.md",
                "--axis",
                "spec",
                "--resume-session",
                "session-ticket-50",
            ]
        )
        self.assertEqual(args.resume_session, "session-ticket-50")

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

    def test_review_prompts_carry_the_axis_brief_and_no_rounds_contract(self):
        preparation = self.bridge.ReviewPreparation(
            fixed_point="main",
            resolved_fixed_point="abc123",
            commit_list="def456 feature change",
            spec_source="docs/feature.md",
            spec_contents="Feature spec.",
            standards_files=("AGENTS.md",),
        )
        prompt = self.bridge.build_prompt(
            base_args(preparation=preparation), "bridge-1"
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
        self.assertIn("Diff: git diff main...HEAD", prompt)

    def test_parser_carries_no_environment_specific_probe(self):
        with self.assertRaises(SystemExit):
            self.bridge.build_parser().parse_args(["--network-probe", "HEAD"])

    def test_health_probes_need_no_review_preparation_inputs(self):
        for flag in ("--probe", "--browser-probe"):
            with self.subTest(flag=flag):
                args = self.bridge.parse_args([flag])

                self.assertTrue(args.probe or args.browser_probe)
                self.assertIsNone(args.base)
                self.assertIsNone(args.spec)

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


MARKER_PATTERN = re.compile(r"\[claude-tui-review-bridge:[^\]]+\]")


class FakeCodexAxis:
    """One fake pane/app-server pair in the multi-pane harness."""

    def __init__(self, owner, axis, pane_id, socket_path):
        self.owner = owner
        self.axis = axis
        self.pane_id = pane_id
        self.socket_path = str(socket_path)
        self.thread_id = f"thread-{axis}-{pane_id.lstrip('%')}"
        self.turn_id = f"turn-{axis}-{pane_id.lstrip('%')}"
        self.marker = None
        self.mcp_startup = {}

    async def pump(self, _seconds):
        error = self.owner.mcp_errors.get(self.axis)
        if error is not None:
            raise RuntimeError(error)
        self.mcp_startup["graph"] = "ready"

    def turn(self):
        status, final_message = self.owner.outcome_for(self.axis)
        if (
            self.owner.concurrent_turn_count
            and len(self.owner.started_turns) < self.owner.concurrent_turn_count
        ):
            status, final_message = "in_progress", ""
        return {
            "id": self.turn_id,
            "status": status,
            "items": [
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": self.marker}],
                },
                {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": final_message,
                },
            ],
        }

    async def request(self, method, params):
        if method == "mcpServerStatus/list":
            return {"data": [{"name": "graph"}]}
        if method == "thread/start":
            error = self.owner.thread_start_errors.get(self.axis)
            if error is not None:
                raise RuntimeError(error)
            return {"thread": {"id": self.thread_id}}
        if method == "thread/resume":
            self.owner.resumed_threads.append(params["threadId"])
            return {}
        if method == "thread/read":
            error = self.owner.axis_errors.get(self.axis)
            if error is not None:
                if isinstance(error, BaseException):
                    raise error
                raise RuntimeError(error)
            return {"thread": {"id": self.thread_id, "turns": [self.turn()]}}
        if method == "turn/start":
            self.owner.started_turns.append(params)
            self.marker = MARKER_PATTERN.search(
                params["input"][0]["text"]
            ).group(0)
            return {"turn": {"id": self.turn_id}}
        raise AssertionError(f"unexpected request: {method}")

    async def __aexit__(self, *_ignored):
        return None


class FakeCodexSession:
    """All fake panes and app-servers launched by one Bridge call."""

    def __init__(self):
        self.panes = []
        self.launched_panes = []
        self.launches = []
        self.started_turns = []
        self.resumed_threads = []
        self.sessions = {}
        self.default_outcome = ("in_progress", "")
        self.axis_outcomes = {}
        self.axis_errors = {}
        self.thread_start_errors = {}
        self.mcp_errors = {}
        self.concurrent_turn_count = 0

    @property
    def first_session(self):
        return next(iter(self.sessions.values()), None)

    @property
    def marker(self):
        return self.first_session.marker if self.first_session else None

    @property
    def thread_id(self):
        return (
            self.first_session.thread_id
            if self.first_session
            else "thread-standards-90"
        )

    @property
    def status(self):
        return self.default_outcome[0]

    @status.setter
    def status(self, value):
        self.default_outcome = (value, self.default_outcome[1])
        self.axis_outcomes = {
            axis: (value, final_message)
            for axis, (_status, final_message) in self.axis_outcomes.items()
        }

    def launch_pane(self, args, runtime_dir):
        runtime_dir = pathlib.Path(runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        socket_path = runtime_dir / "app-server.sock"
        socket_path.touch()
        pane_id = f"%{90 + len(self.launched_panes)}"
        axis = args.axis
        session = FakeCodexAxis(self, axis, pane_id, socket_path)
        self.sessions[str(socket_path)] = session
        self.panes.append(pane_id)
        self.launched_panes.append(pane_id)
        self.launches.append(
            (axis, args.tmux_target, getattr(args, "split_direction", "horizontal"))
        )
        return pane_id

    def close_pane(self, pane_id):
        if pane_id in self.panes:
            self.panes.remove(pane_id)

    def client(self, socket_path):
        return self.sessions[str(socket_path)]

    def outcome_for(self, axis):
        return self.axis_outcomes.get(axis, self.default_outcome)

    def finish(self, message, axis=None):
        if axis is None:
            self.default_outcome = ("completed", message)
        else:
            self.axis_outcomes[axis] = ("completed", message)

    def error(self, axis, reason):
        self.axis_errors[axis] = reason

    def fail_thread_start(self, axis, reason):
        self.thread_start_errors[axis] = reason

    def fail_mcp_startup(self, axis, reason):
        self.mcp_errors[axis] = reason


class DriverKilled(BaseException):
    """The process vanished, so in-process exception cleanup cannot run."""


class FakePaneTestCase(unittest.TestCase):
    """One stubbed Codex pane, driven through the bridge's own entry points.

    Everything the bridge would reach the machine through — the pane, the
    app-server connection, the worktree identity — is stubbed, so a test drives
    a whole review and asserts on what the bridge left behind.
    """

    TMUX = "/private/tmp/tmux-501/default,11028,2"
    ORIGIN_PANE = "%235"

    def setUp(self):
        self.bridge = load_bridge()
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = pathlib.Path(self.work.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.fixed_point = initialize_review_repo(self.worktree)
        self.state_dir = self.root / "state"
        self.codex = FakeCodexSession()

        self.environment = {
            "TMUX": self.TMUX,
            "TMUX_PANE": self.ORIGIN_PANE,
            "CODE_REVIEW_TUI_STATE_DIR": str(self.state_dir),
        }
        self.worktree_root = str(self.worktree)
        self.enter(mock.patch.dict(os.environ, self.environment, clear=False))
        self.enter(mock.patch.object(
            self.bridge, "canonical_worktree_root",
            side_effect=lambda _cwd: self.worktree_root,
        ))
        self.enter(mock.patch.object(
            self.bridge, "launch_pane", self.codex.launch_pane
        ))
        self.enter(mock.patch.object(
            self.bridge, "pane_exists",
            side_effect=lambda pane: pane in self.codex.panes,
        ))
        self.enter(mock.patch.object(
            self.bridge, "close_pane",
            side_effect=self.codex.close_pane,
        ))
        self.enter(mock.patch.object(
            self.bridge, "connect_when_ready", self.connect
        ))
        self.enter(mock.patch.object(
            self.bridge, "connect_existing_session", self.connect_existing
        ))

    def enter(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    async def connect(self, socket_path, *_args, **_kwargs):
        return self.codex.client(socket_path)

    async def connect_existing(self, state):
        if state["paneId"] not in self.codex.panes:
            return None
        return self.codex.client(state["socketPath"])

    def args(self, **overrides):
        values = {
            "base": self.fixed_point,
            "spec": "spec.md",
            "axis": "standards",
            "cwd": str(self.worktree),
            "timeout": 5,
            "startup_timeout": 5,
        }
        values.update(overrides)
        return base_args(**values)

    def install_graph_stub(self, graph_result, graph_status=None):
        graph_bin = self.root / "graph-bin"
        graph_bin.mkdir()
        call_log = self.root / "graph-calls.jsonl"
        executable = graph_bin / "code-review-graph"
        encoded_result = json.dumps(graph_result)
        encoded_status = json.dumps(
            graph_status
            if graph_status is not None
            else {
                "nodes": 1,
                "files": 1,
                "built_at_commit": "graph-built-commit",
            }
        )
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            "with pathlib.Path(os.environ['GRAPH_CALL_LOG']).open(\n"
            "    'a', encoding='utf-8'\n"
            ") as stream:\n"
            "    stream.write(json.dumps({\n"
            "        'argv': sys.argv[1:],\n"
            "        'cwd': os.getcwd(),\n"
            "        'dataDirEnv': os.environ.get('CRG_DATA_DIR'),\n"
            "    }) + '\\n')\n"
            "if sys.argv[1] == 'status':\n"
            f"    print({encoded_status!r})\n"
            "elif sys.argv[1] == 'detect-changes':\n"
            f"    print({encoded_result!r})\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        self.enter(
            mock.patch.dict(
                os.environ,
                {
                    "PATH": f"{graph_bin}{os.pathsep}{os.environ['PATH']}",
                    "GRAPH_CALL_LOG": str(call_log),
                },
                clear=False,
            )
        )
        return call_log

    def use_graphless_path(self):
        graphless_bin = self.root / "graphless-bin"
        graphless_bin.mkdir()
        git = shutil.which("git")
        self.assertIsNotNone(git)
        (graphless_bin / "git").symlink_to(git)
        self.enter(
            mock.patch.dict(
                os.environ,
                {"PATH": str(graphless_bin)},
                clear=False,
            )
        )

    def run_bridge(self, args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = asyncio.run(self.bridge.run_bridge(args))
        printed = stdout.getvalue().strip()
        return code, json.loads(printed) if printed else None

    def kill_the_driver(self):
        """The first review, killed the way the harness kills it."""
        self.codex.error("standards", DriverKilled())
        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args())
        del self.codex.axis_errors["standards"]
        return self.stored_session()

    def stored_session(self):
        return self.stored_sessions()[0]

    def stored_sessions(self, expected_count=1):
        records = sorted(self.state_dir.glob("*.json"))
        self.assertEqual(len(records), expected_count, records)
        return [
            json.loads(record.read_text(encoding="utf-8"))
            for record in records
        ]


class PreparationTests(FakePaneTestCase):
    def test_the_graph_cli_runs_once_and_its_navigation_reaches_the_brief(self):
        (self.worktree / ".code-review-graph").mkdir()
        call_log = self.install_graph_stub(
            {
                "risk_score": 0.93,
                "test_gaps": [{"name": "feature"}],
                "context_savings": {"estimated_tokens_saved": 1200},
                "changed_functions": [
                    {
                        "file_path": str(self.worktree / "feature.py"),
                        "line_start": 1,
                        "line_end": 1,
                        "name": "feature",
                        "risk_score": 0.93,
                    }
                ],
                "review_priorities": [],
            }
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        for call in calls:
            self.assertEqual(
                pathlib.Path(
                    call["argv"][call["argv"].index("--repo") + 1]
                ).resolve(),
                self.worktree.resolve(),
            )
            self.assertEqual(
                pathlib.Path(call["cwd"]).resolve(), self.worktree.resolve()
            )
        for call in calls[1:]:
            self.assertEqual(
                call["argv"][call["argv"].index("--base") + 1],
                self.fixed_point,
            )
        self.assertIn("--json", calls[0]["argv"])
        self.assertIn("--brief", calls[1]["argv"])
        self.assertNotIn("--brief", calls[2]["argv"])
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn(
            "Start here (from the code graph; the diff is the full scope):\n"
            "feature.py:1–1  feature",
            prompt,
        )
        for excluded in ("risk_score", "test_gaps", "context_savings"):
            self.assertNotIn(excluded, prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_an_empty_graph_result_contributes_no_navigation(self):
        (self.worktree / ".code-review-graph").mkdir()
        self.install_graph_stub(
            {"changed_functions": [], "review_priorities": []}
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

    def test_the_cli_resolves_a_graph_outside_the_main_checkout(self):
        external_graph = self.root / "external-graph"
        external_graph.mkdir()
        self.enter(
            mock.patch.dict(
                os.environ,
                {"CRG_DATA_DIR": str(external_graph)},
                clear=False,
            )
        )
        call_log = self.install_graph_stub(
            {
                "changed_functions": [
                    {
                        "file_path": str(self.worktree / "feature.py"),
                        "line_start": 1,
                        "line_end": 1,
                        "name": "feature",
                    }
                ],
                "review_priorities": [],
            }
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        for call in calls:
            self.assertEqual(call["dataDirEnv"], str(external_graph))
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn("feature.py:1–1  feature", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_a_main_checkout_with_no_graph_is_treated_as_unavailable(self):
        call_log = self.install_graph_stub(
            {"changed_functions": [], "review_priorities": []},
            graph_status={
                "nodes": 0,
                "files": 0,
                "last_updated": None,
                "built_on_branch": None,
                "built_at_commit": None,
            },
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual([call["argv"][0] for call in calls], ["status"])
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

    def test_an_unreadable_graph_status_contributes_no_navigation(self):
        call_log = self.install_graph_stub(
            {"changed_functions": [], "review_priorities": []},
            graph_status=[],
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual([call["argv"][0] for call in calls], ["status"])
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertIs(output["preparation"]["codeGraphUsed"], False)

    def test_an_absent_graph_cli_leaves_the_brief_unchanged(self):
        (self.worktree / ".code-review-graph").mkdir()
        self.use_graphless_path()
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertFalse(output["preparation"]["codeGraphUsed"])

    def test_a_dirty_worktree_never_queries_or_mentions_the_graph(self):
        (self.worktree / ".code-review-graph").mkdir()
        call_log = self.install_graph_stub(
            {"changed_functions": [], "review_priorities": []}
        )
        (self.worktree / "pending.txt").write_text(
            "uncommitted\n", encoding="utf-8"
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="standards"))

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertFalse(call_log.exists())
        self.assertNotIn("Start here (from the code graph", prompt)
        self.assertFalse(output["preparation"]["codeGraphUsed"])

    def test_a_linked_worktree_queries_the_graph_at_the_main_checkout(self):
        main_checkout = self.worktree
        linked_worktree = self.root / "linked-worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(main_checkout),
                "worktree",
                "add",
                "--quiet",
                "-b",
                "feature-graph",
                str(linked_worktree),
                "HEAD",
            ],
            check=True,
        )
        (main_checkout / ".code-review-graph").mkdir()
        self.worktree = linked_worktree
        self.worktree_root = str(linked_worktree)
        call_log = self.install_graph_stub(
            {
                "changed_functions": [
                    {
                        "file_path": str(main_checkout / "feature.py"),
                        "line_start": 1,
                        "line_end": 1,
                        "name": "feature",
                    }
                ],
                "review_priorities": [],
            }
        )
        self.codex.finish("no findings")

        code, output = self.run_bridge(self.args(axis="spec"))

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        expected_base = f"{self.fixed_point}...feature-graph"
        self.assertEqual(code, 0)
        self.assertEqual(
            [call["argv"][0] for call in calls],
            ["status", "update", "detect-changes"],
        )
        for call in calls:
            self.assertEqual(
                pathlib.Path(call["cwd"]).resolve(), main_checkout.resolve()
            )
            self.assertEqual(
                pathlib.Path(
                    call["argv"][call["argv"].index("--repo") + 1]
                ).resolve(),
                main_checkout.resolve(),
            )
        for call in calls[1:]:
            self.assertEqual(
                call["argv"][call["argv"].index("--base") + 1],
                expected_base,
            )
        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertIn("feature.py:1–1  feature", prompt)
        self.assertTrue(output["preparation"]["codeGraphUsed"])

    def test_an_unresolvable_fixed_point_fails_before_a_pane_opens(self):
        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "fixed point did not resolve: missing-ref"
            ):
                self.run_bridge(self.args(base="missing-ref"))

        self.assertEqual(self.codex.panes, [])

    def test_an_empty_three_dot_diff_fails_before_a_pane_opens(self):
        fixed_point = subprocess.run(
            ["git", "-C", str(self.worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(RuntimeError, "three-dot diff is empty"):
                self.run_bridge(self.args(base=fixed_point))

        self.assertEqual(self.codex.panes, [])

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

    def test_a_symbolic_fixed_point_is_resolved_only_for_the_report(self):
        self.codex.finish("no findings")

        code, output = self.run_bridge(
            self.args(base="HEAD~1", spec="spec.md", axis="standards")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["fixedPoint"], self.fixed_point)
        self.assertIn("Diff: git diff HEAD~1...HEAD", prompt)

    def test_the_spec_axis_without_a_spec_fails_before_a_pane_opens(self):
        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "run with --axis standards when no spec exists"
            ):
                self.run_bridge(self.args(spec=None, axis="spec"))

        self.assertEqual(self.codex.panes, [])

    def test_both_axes_without_a_spec_fail_before_a_pane_opens(self):
        with self.assertRaisesRegex(
            RuntimeError, "run with --axis standards when no spec exists"
        ):
            self.run_bridge(self.args(spec=None, axis="both"))

        self.assertEqual(self.codex.launched_panes, [])

    def test_an_unreadable_spec_fails_before_a_pane_opens(self):
        (self.worktree / "invalid-spec.md").write_bytes(b"\xff")

        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(RuntimeError, "spec file could not be read"):
                self.run_bridge(self.args(spec="invalid-spec.md", axis="spec"))

        self.assertEqual(self.codex.panes, [])

    def test_a_missing_spec_path_is_not_reported_as_an_issue(self):
        with mock.patch.object(
            self.bridge,
            "launch_pane",
            side_effect=AssertionError("a pane opened"),
        ):
            with self.assertRaisesRegex(RuntimeError, "spec file not found"):
                self.run_bridge(self.args(spec="docs/missing.md", axis="spec"))

        self.assertEqual(self.codex.panes, [])

    def test_a_spec_run_sends_only_the_spec_axis_brief_to_one_pane(self):
        fixed_point = self.fixed_point
        self.codex.finish("no findings")

        code, _output = self.run_bridge(
            self.args(base=fixed_point, spec="spec.md", axis="spec")
        )

        prompt = self.codex.started_turns[0]["input"][0]["text"].split("\n", 1)[1]
        self.assertEqual(code, 0)
        self.assertEqual(self.codex.panes, [])
        self.assertIn("Spec:\nFeature spec.\n\nReport:", prompt)
        self.assertNotIn("Smell baseline", prompt)
        for excluded in (
            "Rounds contract",
            "one re-review",
            "$code-review",
            "/code-review",
            "mattpocock-skills",
            "Start here (from the code graph",
        ):
            self.assertNotIn(excluded, prompt)

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

    def test_an_issue_spec_reaches_the_spec_axis_with_its_comments(self):
        real_run = subprocess.run

        def run(command, **kwargs):
            if command[:3] == ["gh", "issue", "view"]:
                self.assertEqual(command, ["gh", "issue", "view", "17", "--comments"])
                return argparse.Namespace(
                    returncode=0,
                    stdout="Issue title\n\nIssue body\n\nFirst comment\n",
                    stderr="",
                )
            return real_run(command, **kwargs)

        self.codex.finish("no findings")
        with mock.patch.object(self.bridge.subprocess, "run", side_effect=run):
            code, output = self.run_bridge(
                self.args(spec="17", axis="spec")
            )

        prompt = self.codex.started_turns[0]["input"][0]["text"].split("\n", 1)[1]
        self.assertEqual(code, 0)
        self.assertEqual(output["preparation"]["specSource"], "17")
        self.assertIn(
            "Spec:\nIssue title\n\nIssue body\n\nFirst comment\n\nReport:",
            prompt,
        )


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

    def test_a_killed_driver_leaves_a_recoverable_record_and_a_live_pane(self):
        state = self.kill_the_driver()

        self.assertEqual(self.codex.panes, ["%90"])
        self.assertEqual(state["threadId"], self.codex.thread_id)
        self.assertEqual(state["marker"], self.codex.marker)
        self.assertEqual(state["owner"]["origin_pane"], self.ORIGIN_PANE)

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
        killed = self.kill_the_driver()
        self.codex.finish("round one findings")
        self.run_bridge(self.args(recover_session=True))
        turns_before = len(self.codex.started_turns)

        code, output = self.run_bridge(
            self.args(resume_session=killed["reviewSessionId"])
        )

        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(
            len(self.codex.started_turns) - turns_before,
            1,
            "round two should start exactly one follow-up turn",
        )
        self.assertEqual(self.codex.panes, [])

    def test_two_recovered_axes_each_remain_resumable_for_round_two(self):
        killed = self.kill_the_two_axis_driver()
        self.codex.finish("round one standards", axis="standards")
        self.codex.finish("round one spec", axis="spec")
        self.run_bridge(self.args(recover_session=True))
        turns_before = len(self.codex.started_turns)

        for axis in ("standards", "spec"):
            self.codex.finish(f"round two {axis}", axis=axis)
            code, output = self.run_bridge(
                self.args(
                    axis=axis,
                    resume_session=killed[axis]["reviewSessionId"],
                )
            )

            self.assertEqual(code, 0)
            result = output["axes"][axis]
            self.assertEqual(
                result["reviewSessionId"], killed[axis]["reviewSessionId"]
            )
            self.assertEqual(result["finalMessage"], f"round two {axis}")

        self.assertEqual(len(self.codex.started_turns) - turns_before, 2)
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
            "tui_review_bridge.py", "--recover-session", "--cwd", str(self.worktree),
        ]):
            code = self.bridge.main()

        self.assertEqual(code, self.bridge.NO_LIVE_SESSION_EXIT)
        self.assertNotEqual(self.bridge.NO_LIVE_SESSION_EXIT, 1)
        self.assertEqual(self.codex.panes, [])


def token_count(usage):
    """One `token_count` event, in the shape a Codex rollout writes it."""
    return {
        "timestamp": "2026-08-18T04:00:00.000Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": usage}},
    }


def turn_context(model):
    """One `turn_context` line, which is where a rollout says which model the turn resolved to."""
    return {
        "timestamp": "2026-08-18T04:00:00.000Z",
        "type": "turn_context",
        "payload": {"model": model, "cwd": "/workspace"},
    }


def write_rollout(root, thread_id, records, day="2026/08/18"):
    """One rollout file, under the dated tree and named the way Codex names it."""
    directory = root / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-18T04-00-00-{thread_id}.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


# One round's cumulative counters, as Codex reports them: `input_tokens` counts the cached ones
# inside itself, and `reasoning_output_tokens` counts a subset of `output_tokens`.
ROUND_ONE_USAGE = {
    "input_tokens": 200000,
    "cached_input_tokens": 180000,
    "cache_write_input_tokens": 0,
    "output_tokens": 9000,
    "reasoning_output_tokens": 6000,
    "total_tokens": 209000,
}
ROUND_TWO_USAGE = {
    "input_tokens": 350000,
    "cached_input_tokens": 310000,
    "cache_write_input_tokens": 0,
    "output_tokens": 15000,
    "reasoning_output_tokens": 9000,
    "total_tokens": 365000,
}
# The same figures after the mapping the spec pins: cache-read is the cached count, input is what
# is left of the input count once the cached ones are out of it, cache-creation is the write
# count, and output keeps its reasoning tokens inside it rather than beside them.
ROUND_ONE_COUNTERS = {
    "input": 20000, "output": 9000, "cache_read": 180000, "cache_creation": 0,
}
ROUND_TWO_COUNTERS = {
    "input": 40000, "output": 15000, "cache_read": 310000, "cache_creation": 0,
}
RESOLVED_MODEL = "gpt-5.6-sol"


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



class RecoveryParserTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def test_recovery_needs_no_preparation_inputs(self):
        args = self.bridge.parse_args(["--recover-session"])

        self.assertTrue(args.recover_session)
        self.assertIsNone(args.base)
        self.assertIsNone(args.spec)

    def test_a_review_still_requires_its_fixed_point(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(["--cwd", "/workspace/ticket-13"])

    def test_recovery_and_resume_are_exclusive(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(
                ["--recover-session", "--resume-session", "session-13"]
            )

    def test_recovery_takes_no_free_text_target(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(["--recover-session", "HEAD"])


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


class LifecycleHookTests(FakePaneTestCase):
    """The four points a caller can hand a command for, and what each is told.

    The bridge learns no caller's vocabulary: a hook command is composed by the
    caller and carries whatever correlation token that caller needs, and the only
    thing the bridge adds is facts it owns. So these tests hand in a real command
    at each point and pin the firings and the environment they arrive with.
    """

    MODEL = "gpt-5.6-luna"

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
        argv = ["tui_review_bridge.py", "--cwd", str(self.worktree), *arguments]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(stdout):
                code = self.bridge.main()
        printed = stdout.getvalue().strip()
        self.output = json.loads(printed) if printed else None
        return code

    def review_argv(self, *arguments, timeout="5", axis="standards", hooks=None):
        return (
            "--base", self.fixed_point,
            "--spec", "spec.md",
            "--axis", axis,
            "--model", self.MODEL,
            "--timeout", timeout,
            "--startup-timeout", "5",
            *(self.hooks() if hooks is None else hooks),
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
        """Round one closed its pane, so round two opens one of its own."""
        self.codex.finish("round one findings")
        self.main(*self.review_argv())
        session = self.stored_session()["reviewSessionId"]
        self.firings.unlink()

        code = self.main(*self.review_argv("--resume-session", session))

        self.assertEqual(code, 0)
        self.assertEqual(
            self.events(),
            ["review-start", "child-launch", "axis-end", "review-end"],
        )

    def test_a_resume_onto_a_live_pane_launches_no_further_child(self):
        """Nothing was launched, so nothing is announced as launched."""
        state = self.kill_the_driver()
        self.codex.finish("round two clear")

        code = self.main(
            *self.review_argv("--resume-session", state["reviewSessionId"])
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


class LifecycleHookParserTests(unittest.TestCase):
    """The arguments the protocol is handed in through, and the ones it retired."""

    def setUp(self):
        self.bridge = load_bridge()

    def test_the_bridge_takes_no_argument_naming_a_caller_log_or_ticket(self):
        for argument in ("--machine-log", "--ticket"):
            with self.subTest(argument=argument):
                with self.assertRaises(SystemExit):
                    self.bridge.parse_args(
                        ["--base", "main", argument, "anything"]
                    )

    def test_each_lifecycle_point_takes_one_command(self):
        args = self.bridge.parse_args([
            "--base", "main",
            "--on-child-launch", "notify launch",
            "--on-review-start", "notify start",
            "--on-axis-end", "notify axis",
            "--on-review-end", "notify end",
        ])

        self.assertEqual(args.on_child_launch, "notify launch")
        self.assertEqual(args.on_review_start, "notify start")
        self.assertEqual(args.on_axis_end, "notify axis")
        self.assertEqual(args.on_review_end, "notify end")

    def test_a_point_left_out_carries_no_command(self):
        args = self.bridge.parse_args(["--base", "main"])

        self.assertIsNone(args.on_child_launch)
        self.assertIsNone(args.on_review_start)
        self.assertIsNone(args.on_axis_end)
        self.assertIsNone(args.on_review_end)


if __name__ == "__main__":
    unittest.main()
