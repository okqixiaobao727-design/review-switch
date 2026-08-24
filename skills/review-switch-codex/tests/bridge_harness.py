#!/usr/bin/env python3
"""The fakes and fixtures every Bridge test file is driven through."""

import argparse
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
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


class FakeClient:
    def __init__(self):
        self.requests = []

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn-followup"}}
        raise AssertionError(f"unexpected request: {method}")


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
