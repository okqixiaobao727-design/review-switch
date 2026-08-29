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
    pathlib.Path(__file__).parents[1] / "review_bridge.py"
)


def load_bridge():
    spec = importlib.util.spec_from_file_location("review_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StaticBrief:
    """One Axis Brief, in the shape a Lane reads it."""

    def __init__(self, axis):
        self.axis = axis
        self.text = f"Read-only {axis} review"


class StaticPreparation:
    def brief(self, axis):
        return StaticBrief(axis)

    def briefs(self, axes):
        return tuple(self.brief(axis) for axis in axes)

    def report(self):
        return {
            "fixedPoint": "resolved-main",
            "specSource": "docs/feature.md",
            "specFile": "docs/feature.md",
            "specFailure": None,
            "standardsFiles": [],
            "standardsCondition": "absent",
            "codeGraphUsed": False,
            "responseFile": None,
        }


def base_args(**overrides):
    values = {
        "reviewer": "codex",
        "base": "main",
        "spec": "docs/feature.md",
        "axis": "standards",
        "preparation": StaticPreparation(),
        "cwd": "/workspace/ticket-50",
        "timeout": 1,
        "startup_timeout": 1,
        "parent_pid": os.getpid(),
        "sandbox": "danger-full-access",
        "approval": "never",
        "network": False,
        "tmux_target": None,
        "resume_session": None,
        "response": None,
        "recover_session": False,
        "model": None,
        "effort": None,
        "standards_model": None,
        "standards_effort": None,
        "spec_model": None,
        "spec_effort": None,
        "probe": False,
        "browser_probe": False,
        "account": None,
        "claude_binary": None,
        "resume_state": None,
        "resume_thread_id": None,
        "status": "failed",
    }
    values.update(overrides)
    if "caller_arguments" not in overrides:
        arguments = [
            "--reviewer", values["reviewer"],
            "--cwd", values["cwd"],
            "--base", values["base"],
        ]
        if values["spec"] is not None:
            arguments.extend(["--spec", values["spec"]])
        arguments.append("--network" if values["network"] else "--no-network")
        values["caller_arguments"] = arguments
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
        if method == "thread/queue/add":
            return {"queuedSubmission": {"id": "queued-followup"}}
        raise AssertionError(f"unexpected request: {method}")


MARKER_PATTERN = re.compile(r"\[claude-tui-review-bridge:[^\]]+\]")


class FakeCodexAxis:
    """One fake pane/app-server pair in the multi-pane harness."""

    def __init__(
        self, owner, axis, pane_id, socket_path, model=None, effort=None,
        resume_thread_id=None,
    ):
        self.owner = owner
        self.axis = axis
        self.pane_id = pane_id
        self.socket_path = str(socket_path)
        self.thread_id = f"thread-{axis}-{pane_id.lstrip('%')}"
        self.turn_id = f"turn-{axis}-{pane_id.lstrip('%')}"
        self.model = model
        self.effort = effort
        self.resume_thread_id = resume_thread_id
        self.marker = None
        self.tui_attached = False
        self.queued = []
        # Kept so this harness can exercise the pre-#26 notification gate when
        # the regression test is run against the fixed point.
        self.mcp_startup = {}

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

    def record_turn(self, params):
        self.owner.started_turns.append(params)
        self.marker = MARKER_PATTERN.search(
            params["input"][0]["text"]
        ).group(0)

    def attach_tui(self, thread_id=None):
        """Attach the visible control client while this thread is still idle.

        Codex 0.149.1 interrupts an already-active turn when the TUI resumes that
        thread. Modelling that control effect keeps the delivery fake honest.
        """
        if self.tui_attached:
            return
        if thread_id is not None:
            self.thread_id = thread_id
            self.owner.resumed_threads.append(thread_id)
        self.tui_attached = True
        self.owner.tui_attachments.append((self.axis, self.thread_id))
        self.owner.control_events.append((self.axis, "tui-attached"))
        if self.marker is not None:
            self.owner.axis_errors[self.axis] = "Cannot write to closing transport"
        self.owner.record_mcp_startup(self)

    async def request(self, method, params):
        if method == "thread/loaded/list":
            self.attach_tui(self.resume_thread_id)
            return {"data": [self.thread_id]}
        if method == "mcpServerStatus/list":
            return {"data": [{"name": "graph"}]}
        if method == "config/read":
            error = self.owner.mcp_errors.get(self.axis)
            if error is not None:
                raise RuntimeError(error)
            return {
                "config": {"mcp_servers": {"graph": {"enabled": True}}}
            }
        if method == "thread/start":
            error = self.owner.thread_start_errors.get(self.axis)
            if error is not None:
                raise RuntimeError(error)
            return {"thread": {"id": self.thread_id}}
        if method == "thread/resume":
            self.owner.resumed_threads.append(params["threadId"])
            self.thread_id = params["threadId"]
            return {}
        if method == "thread/read":
            error = self.owner.axis_errors.get(self.axis)
            if error is not None:
                if isinstance(error, BaseException):
                    raise error
                raise RuntimeError(error)
            turns = [self.turn()] if self.marker is not None else []
            return {
                "thread": {
                    "id": self.thread_id,
                    "status": {"type": "idle"},
                    "turns": turns,
                }
            }
        if method == "thread/queue/list":
            return {"data": list(self.queued), "nextCursor": None}
        if method == "thread/queue/add":
            submission = {
                "id": f"queued-{self.axis}-{len(self.owner.started_turns) + 1}",
                "clientUserMessageId": params["clientUserMessageId"],
                "input": params["input"],
            }
            self.queued.append(submission)
            self.owner.control_events.append((self.axis, "brief-queued"))
            effective = dict(params)
            if self.model:
                effective["model"] = self.model
            if self.effort:
                effective["effort"] = self.effort
            self.record_turn(effective)
            self.queued.remove(submission)
            if self.owner.queue_add_exit_after_accept is not None:
                raise self.owner.queue_add_exit_after_accept
            return {"queuedSubmission": submission}
        if method == "turn/start":
            self.record_turn(params)
            return {"turn": {"id": self.turn_id}}
        raise AssertionError(f"unexpected request: {method}")

    async def pump(self, _seconds):
        """Settle the fixed point's notification gate without clock sleeps."""
        self.mcp_startup["graph"] = "ready"

    async def __aexit__(self, *_ignored):
        self.owner.closed_clients.append((self.axis, self.pane_id))
        return None


class FakeCodexSession:
    """All fake panes and app-servers launched by one Bridge call."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.panes = []
        self.launched_panes = []
        self.launches = []
        self.started_turns = []
        self.tui_attachments = []
        self.control_events = []
        self.closed_clients = []
        self.resumed_threads = []
        self.sessions = {}
        self.default_outcome = ("in_progress", "")
        self.axis_outcomes = {}
        self.axis_errors = {}
        self.thread_start_errors = {}
        self.mcp_errors = {}
        self.queue_add_exit_after_accept = None
        self.concurrent_turn_count = 0

    def record_mcp_startup(self, session):
        if hasattr(self.bridge, "record_mcp_startup_notification"):
            self.bridge.record_mcp_startup_notification(
                pathlib.Path(session.socket_path).parent
                / self.bridge.MCP_STARTUP_LOG_FILENAME,
                {
                    "method": "mcpServer/startupStatus/updated",
                    "params": {
                        "threadId": session.thread_id,
                        "name": "graph",
                        "status": "ready",
                    },
                },
            )
        else:
            session.mcp_startup["graph"] = "ready"
        self.control_events.append((session.axis, "mcp-ready"))

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
        session = FakeCodexAxis(
            self,
            axis,
            pane_id,
            socket_path,
            getattr(args, "model", None),
            getattr(args, "effort", None),
            getattr(args, "resume_thread_id", None),
        )
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

    def hand_off_thread(self, runtime_dir, thread_id):
        """Model the old pane consuming its handoff and resuming an active turn."""
        socket_path = str(pathlib.Path(runtime_dir) / "app-server.sock")
        self.sessions[socket_path].attach_tui(thread_id)


class FakeClaudeProcess:
    """One stubbed headless Claude process, in the shape the Lane drives it.

    A headless reviewer's whole life is visible from outside: it is launched, it
    is alive or it is not, and when it exits its one JSON object is on disk. That
    is all this stands in for.
    """

    def __init__(self, session, axis, pid, runtime_dir, command):
        self.session = session
        self.axis = axis
        self.pid = pid
        self.runtime_dir = pathlib.Path(runtime_dir)
        self.command = command
        self.completed = False

    @property
    def prompt(self):
        """The prompt this process was launched with, which is its last argument."""
        return self.command[-1]

    @property
    def result_path(self):
        return self.runtime_dir / "result.json"

    def exit_with_result(self):
        """Write what this process prints on its way out, and stop being alive."""
        if self.completed:
            return
        if not self.runtime_dir.is_dir():
            # The runtime directory is gone, so this reviewer's axis has been
            # settled and there is nothing left of it to print into.
            self.completed = True
            self.session.alive.discard(self.pid)
            return
        printed = self.session.printed_by(self.axis)
        if printed is None:
            return
        self.result_path.write_text(printed, encoding="utf-8")
        self.completed = True
        self.session.alive.discard(self.pid)

    async def wait(self, _timeout):
        error = self.session.axis_errors.get(self.axis)
        if error is not None:
            if isinstance(error, BaseException):
                raise error
            raise RuntimeError(error)
        self.exit_with_result()
        if not self.completed:
            raise TimeoutError(f"the {self.axis} reviewer was still running")

    def kill(self):
        self.session.terminate(self.pid)


class FakeClaudeSession:
    """Every stubbed headless reviewer launched by one Bridge call."""

    def __init__(self):
        self.launched = []
        self.alive = set()
        self.killed = []
        self.default_outcome = None
        self.axis_outcomes = {}
        self.axis_errors = {}
        self.axis_payloads = {}
        self.raw_output = {}
        self.launch_errors = {}
        self.usage = {}
        self.model_usage = {}

    @property
    def commands(self):
        return [process.command for process in self.launched]

    @property
    def prompts(self):
        return [process.prompt for process in self.launched]

    def launch(self, args, command, runtime_dir):
        error = self.launch_errors.pop(args.axis, None)
        if error is not None:
            raise RuntimeError(error)
        pid = 4000 + len(self.launched)
        process = FakeClaudeProcess(self, args.axis, pid, runtime_dir, command)
        self.launched.append(process)
        self.alive.add(pid)
        return process

    def printed_by(self, axis):
        """The stdout this axis's reviewer leaves behind, or `None` while it runs on."""
        if axis in self.raw_output:
            return self.raw_output[axis]
        payload = self.payload_for(axis)
        return None if payload is None else json.dumps(payload)

    def payload_for(self, axis):
        """The JSON object this axis's reviewer prints, or `None` while it runs on."""
        if axis in self.axis_payloads:
            return self.axis_payloads[axis]
        message = self.axis_outcomes.get(axis, self.default_outcome)
        if message is None:
            return None
        payload = {
            "session_id": f"claude-{axis}",
            "result": message,
            "is_error": False,
            "subtype": "success",
            "permission_denials": [],
        }
        if axis in self.usage:
            payload["usage"] = self.usage[axis]
        if axis in self.model_usage:
            payload["modelUsage"] = {self.model_usage[axis]: {"inputTokens": 1}}
        return payload

    def finish(self, message, axis=None):
        """Give a reviewer its report; one already launched exits on it."""
        if axis is None:
            self.default_outcome = message
        else:
            self.axis_outcomes[axis] = message
        self.settle_launched()

    def answer_with(self, payload, axis=None):
        """Give a reviewer a whole JSON object of its own to print."""
        for target in (axis,) if axis else ("standards", "spec"):
            self.axis_payloads[target] = payload
        self.settle_launched()

    def settle_launched(self):
        """Let every reviewer already launched exit on the outcome it now has."""
        for process in self.launched:
            process.exit_with_result()

    def terminate(self, pid):
        """Stop a reviewer, whether its driver holds it or only its number."""
        self.killed.append(pid)
        self.alive.discard(pid)

    def garble(self, axis, printed="not a JSON object at all"):
        """Have a reviewer print something that is not the one JSON object."""
        self.raw_output[axis] = printed
        self.settle_launched()

    def bill(self, usage, model=None, axis="standards"):
        self.usage[axis] = usage
        if model is not None:
            self.model_usage[axis] = model

    def error(self, axis, reason):
        self.axis_errors[axis] = reason

    def fail_launch(self, axis, reason):
        self.launch_errors[axis] = reason


class DriverKilled(BaseException):
    """The process vanished, so in-process exception cleanup cannot run."""


def gh_requested_fields(captured, command):
    """A captured `gh --json` response, cut to the fields this command named.

    `gh` returns exactly the fields asked for and no others, so a caller that
    asks for the wrong ones must not be handed the right ones by the fake.
    """
    if not captured:
        return captured
    payload = json.loads(captured)
    requested = command[command.index("--json") + 1].split(",")
    missing = [field for field in requested if field not in payload]
    if missing:
        raise KeyError(
            f"the captured `gh` response models no {', '.join(missing)}; "
            "capture one that does rather than inventing the field here"
        )
    return json.dumps({field: payload[field] for field in requested})


def graph_navigation_result(*priorities, changed_functions=None, **fields):
    """A `detect-changes` result the Bridge can navigate by.

    The tool ranks a subset of what changed, so a priority is a changed
    function too and both keys carry it. A test that needs a changed function
    the tool ranked nothing in names `changed_functions` itself; that
    asymmetry is the only thing the navigation block turns on (#32).
    """
    return {
        "changed_functions": list(
            priorities if changed_functions is None else changed_functions
        ),
        "review_priorities": list(priorities),
        **fields,
    }


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
        self.codex = FakeCodexSession(self.bridge)
        self.claude = FakeClaudeSession()

        self.environment = {
            "TMUX": self.TMUX,
            "TMUX_PANE": self.ORIGIN_PANE,
            "CODE_REVIEW_TUI_STATE_DIR": str(self.state_dir),
            # A stub Lane launches no real reviewer, but the binary is resolved
            # before one is launched, so it has to name a file that exists.
            "CODE_REVIEW_CLAUDE_BINARY": sys.executable,
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
        self.enter(mock.patch.object(
            self.bridge,
            "hand_off_thread",
            side_effect=self.codex.hand_off_thread,
            create=True,
        ))
        self.claude_launch = self.enter(mock.patch.object(
            self.bridge, "launch_claude", self.claude.launch
        ))
        self.enter(mock.patch.object(
            self.bridge, "claude_process_alive",
            side_effect=lambda pid: pid in self.claude.alive,
        ))
        self.enter(mock.patch.object(
            self.bridge, "terminate_claude", side_effect=self.claude.terminate
        ))

    def enter(self, patcher):
        """Start a patcher for the length of this test, and hand it back.

        Handed back because a test that drives a real reviewer process has to
        stop the stub that stands in for one; stopping is therefore tolerant of
        having already happened.
        """
        patcher.start()
        self.addCleanup(self.stop_patcher, patcher)
        return patcher

    @staticmethod
    def stop_patcher(patcher):
        with contextlib.suppress(RuntimeError):
            patcher.stop()

    def use_real_claude_process(self):
        """Let this test launch a reviewer process for real, stub and all."""
        self.stop_patcher(self.claude_launch)

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
        if values.get("resume_session") and "response" not in overrides:
            values["response"] = self.default_response_file()
        return base_args(**values)

    def default_response_file(self):
        """The Response a resume carries when a test is about something else.

        Every resume a caller makes carries one, so every resume the harness
        makes does too: a test that leaves it out would otherwise drive a
        command line the Bridge refuses (#37). A test that is *about* the
        Response passes its own, or `response=None` for a resume without one.
        """
        path = self.root / "caller-response.md"
        if not path.exists():
            path.write_text(
                '1. "the previous round\'s finding" — fixed in feature.py\n',
                encoding="utf-8",
            )
        return str(path)

    def fake_gh(self, returncode=0, stdout="", stderr="", plain_stdout=""):
        """Stand `gh issue view` up at the process boundary, faithful to its flags.

        `stdout` answers the `--json` form, cut down to the fields that form
        asked for, and `plain_stdout` answers every other. Both halves of that
        are the defect itself: `--comments` returns the thread without the body
        at rc=0, and `--json` returns the named fields and no others. Asking
        the wrong way therefore fails a test on the brief the Lane receives,
        which is where it should fail — no test here asserts how the Bridge
        fetched.

        Faked here rather than on `PATH` because CI has no GitHub
        authentication; every other command still runs for real. The log is
        what lets a test assert a reference never reached GitHub at all.
        """
        real_run = subprocess.run
        calls = []

        def run(command, **kwargs):
            if list(command[:3]) == ["gh", "issue", "view"]:
                calls.append(list(command))
                return argparse.Namespace(
                    returncode=returncode,
                    stdout=(
                        gh_requested_fields(stdout, command)
                        if "--json" in command
                        else plain_stdout
                    ),
                    stderr=stderr,
                )
            return real_run(command, **kwargs)

        self.enter(
            mock.patch.object(self.bridge.subprocess, "run", side_effect=run)
        )
        return calls

    def install_graph_stub(
        self,
        graph_result,
        graph_status=None,
        build_returncode=0,
        build_seconds=0,
    ):
        """Stand the graph CLI up on `PATH`, logging every call it is handed.

        `build_returncode` and `build_seconds` are the two ways a build goes
        wrong — it fails, or it runs past the Bridge's bound — and both are
        answered by the real executable rather than by patching the Bridge, so
        a test asserts on the same process boundary a review crosses.
        """
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
            "import time\n"
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
            f"    print({encoded_result!r})\n"
            "elif sys.argv[1] == 'build':\n"
            f"    time.sleep({build_seconds!r})\n"
            f"    sys.exit({build_returncode!r})\n",
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

    @staticmethod
    def graph_calls(call_log):
        """Every call the stubbed graph CLI recorded, in the order it took them."""
        return [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]

    def use_linked_worktree(self, branch="feature-graph"):
        """Move this test into a linked worktree, and hand back the checkout it left.

        A worktree is reviewed exactly as its main checkout is, so everything
        the harness points at the checkout under review — the `cwd` an
        invocation carries, the worktree root it is keyed by — moves with it.
        """
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
                branch,
                str(linked_worktree),
                "HEAD",
            ],
            check=True,
        )
        self.worktree = linked_worktree
        self.worktree_root = str(linked_worktree)
        return main_checkout

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

    def kill_the_driver(self, axis="standards"):
        """The first review of one axis, killed the way the harness kills it."""
        self.codex.error(axis, DriverKilled())
        with self.assertRaises(DriverKilled):
            self.run_bridge(self.args(axis=axis))
        del self.codex.axis_errors[axis]
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


# One round's counters as a headless Claude result reports them. Claude counts its
# cached tokens beside the input count rather than inside it, so the four arrive
# already disjoint and map straight onto the counters an `axis-end` is handed.
CLAUDE_ROUND_ONE_USAGE = {
    "input_tokens": ROUND_ONE_COUNTERS["input"],
    "output_tokens": ROUND_ONE_COUNTERS["output"],
    "cache_read_input_tokens": ROUND_ONE_COUNTERS["cache_read"],
    "cache_creation_input_tokens": ROUND_ONE_COUNTERS["cache_creation"],
}
