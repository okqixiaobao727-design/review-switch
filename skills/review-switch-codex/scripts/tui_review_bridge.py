#!/usr/bin/env python3

"""Interactive Codex TUI code-review channel for a Claude session.

Round one starts a review lineage in its own tmux pane; round two resumes that
thread, and `--recover-session` re-attaches to the turn a killed driver left in
flight.

Given `--machine-log` and `--ticket`, every call also leaves the run's machine
log the pair of `review` lines the contract describes — `running` on entry and
`returned` on exit — so the dashboard's review annotation appears and disappears
with no operator action and no model token spent (ADR-0001).
"""

import argparse
import asyncio
import dataclasses
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager

try:
    import aiohttp
except ImportError as error:
    raise SystemExit(
        "tui_review_bridge requires Python package 'aiohttp'. "
        "Install it for the Python interpreter used by Claude Code."
    ) from error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import launch_hook  # noqa: E402  — a sibling asset, reached from this file's own directory


TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
# A thread's MCP servers announce themselves on the connection that started the
# thread; the first turn must wait until none of them is still coming up.
MCP_STARTUP_NOTIFICATION = "mcpServer/startupStatus/updated"
MCP_STARTUP_SETTLED_STATES = {"ready", "failed", "cancelled"}
# Servers do not all announce at once — one was measured announcing `starting`
# 169 ms after another had already reported `ready`. When some configured server
# has yet to say anything, the gate waits out this much quiet before believing
# the ones it has heard from are the whole set.
MCP_STARTUP_QUIET_SECONDS = 0.5
# What the pane allows the parent, on top of the two timeouts the parent's own
# steps are bounded by (connecting, then the readiness gate), for the turn it
# submits in between. This is a reaper for a parent that died without cleaning
# up — a parent that fails in the ordinary way kills the pane itself — so it errs
# long: expiring early would tear down an app-server still being set up.
HANDOFF_GRACE_SECONDS = 60
# The parent writes the thread id here once the first turn has given the thread
# a rollout; the pane process waits for it before attaching its TUI.
THREAD_HANDOFF_FILENAME = "thread-id"
DEFAULT_TIMEOUT_SECONDS = 7200
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60
SESSION_STATE_VERSION = 1
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Recovery found nothing to re-attach to, which is a different answer from a
# failed review: it is the one result that licenses starting a first review.
NO_LIVE_SESSION_EXIT = 3
# The log's own writer, so the `review` event's shape and its closed set of
# states stay the log's alone: this bridge names the event, never spells it.
MACHINE_LOG = pathlib.Path(__file__).resolve().parents[2] / "machine_log.py"
# The vendor half of the lane this bridge reviews in. The model half is whatever
# the lineage was pinned to, so the lane needs no argument of its own.
REVIEW_VENDOR = "codex"


class AppServerError(RuntimeError):
    pass


class NoLiveSessionError(RuntimeError):
    """No live review session belongs to the caller's owner identity."""


class ReviewEvent:
    """The pair of `review` lines one bridge call leaves in the run's machine log.

    Both the log path and the ticket are optional everywhere they appear, and a
    call given neither writes nothing: `--log` is optional on dispatch, and a run
    without one still reviews normally.
    """

    def __init__(self, log, ticket, model, vendor=REVIEW_VENDOR):
        # Absolute where the path enters, before it is forwarded to the writer
        # (ADR-0007), so what this bridge records cannot be moved by anyone's
        # working directory. Symlinks are left alone: the operator's own name for
        # the run directory is the one to log under.
        self.log = os.path.abspath(log) if log else None
        self.ticket = ticket
        # Vendor then model, separated by a space: the annotation row prints the
        # field verbatim after collapsing whitespace, so this is the spelling the
        # dashboard shows.
        self.lane = " ".join(part for part in (vendor, model) if part)

    def write(self, state):
        """Append one end of this review; returns nothing, and fails at nothing.

        A review that succeeded must not be reported as a failure because its
        bookkeeping could not be written, so every way the append can go wrong is
        swallowed here: the caller's exit status and the JSON object the reviewed
        child reads are the same either way.
        """
        if not self.log or not self.ticket:
            return
        try:
            subprocess.run(
                [
                    sys.executable, str(MACHINE_LOG), "--log", str(self.log),
                    "review", "--ticket", str(self.ticket), "--lane", self.lane,
                    "--state", state,
                ],
                capture_output=True, text=True, check=False,
            )
        except OSError:
            pass


@dataclasses.dataclass(frozen=True)
class InvocationOwner:
    tmux_server: str
    origin_pane: str
    worktree_root: str

    @property
    def key(self):
        return "\0".join(
            (self.tmux_server, self.origin_pane, self.worktree_root)
        )

    def to_dict(self):
        return dataclasses.asdict(self)


def canonical_worktree_root(cwd):
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return str(pathlib.Path(result.stdout.strip()).resolve())
    return str(pathlib.Path(cwd).resolve())


def tmux_server_identity(value):
    parts = value.rsplit(",", 2)
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise RuntimeError("TMUX does not identify a tmux server")
    return f"{parts[0]},{parts[1]}"


def resolve_owner(args, environment=None):
    environment = os.environ if environment is None else environment
    tmux_value = environment.get("TMUX")
    origin_pane = args.tmux_target or environment.get("TMUX_PANE")
    if not tmux_value or not origin_pane:
        raise RuntimeError(
            "Claude Code must run inside tmux with an originating tmux pane"
        )
    return InvocationOwner(
        tmux_server=tmux_server_identity(tmux_value),
        origin_pane=origin_pane,
        worktree_root=canonical_worktree_root(args.cwd),
    )


def validate_session_owner(state, owner):
    if state.get("owner") != owner.to_dict():
        raise RuntimeError(
            "Review session belongs to another tmux pane or Git worktree"
        )


class SessionStore:
    def __init__(self, root=None):
        if root is None:
            configured = os.environ.get("CODE_REVIEW_TUI_STATE_DIR")
            if configured:
                root = configured
            else:
                claude_root = pathlib.Path(
                    os.environ.get(
                        "CLAUDE_CONFIG_DIR",
                        pathlib.Path.home() / ".claude",
                    )
                )
                root = claude_root / "state" / "code-review-tui"
        self.root = pathlib.Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _safe_id(self, session_id):
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise RuntimeError("Invalid review session id")
        return session_id

    def state_path(self, session_id):
        return self.root / f"{self._safe_id(session_id)}.json"

    def lock_path(self, key):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.lock"

    def write(self, session_id, state):
        destination = self.state_path(session_id)
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=f".{session_id}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with temporary:
                json.dump(state, temporary, ensure_ascii=False, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary.name, 0o600)
            os.replace(temporary.name, destination)
        except Exception:
            pathlib.Path(temporary.name).unlink(missing_ok=True)
            raise

    def read(self, session_id):
        try:
            state = json.loads(
                self.state_path(session_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Unknown review session: {session_id}"
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Unreadable review session: {session_id}"
            ) from error
        if state.get("version") != SESSION_STATE_VERSION:
            raise RuntimeError(
                f"Unsupported review session version: {state.get('version')}"
            )
        return state

    def delete(self, session_id):
        self.state_path(session_id).unlink(missing_ok=True)

    def find_by_owner(self, owner):
        """Returns every recoverable record this owner wrote, newest turn first.

        The record is written before the turn is awaited, so a session whose
        driver died mid-review is already on disk under the same owner tuple the
        resume path validates. A record without a `marker` predates recovery and
        names no turn to wait on, so it is not recoverable; so is anything
        unreadable or written by another version, which is skipped rather than
        raised — one damaged file must not hide a healthy session.
        """
        found = []
        for path in sorted(self.root.glob("*.json")):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict):
                continue
            if state.get("version") != SESSION_STATE_VERSION:
                continue
            if state.get("owner") != owner.to_dict():
                continue
            if not state.get("marker") or not state.get("threadId"):
                continue
            found.append(state)
        found.sort(key=lambda state: state.get("updatedAt") or 0, reverse=True)
        return found


@contextmanager
def owner_lock(store, owner):
    lock_path = store.lock_path(owner.key)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "Another review bridge call is already running for this tmux pane"
            ) from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class AppServerClient:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.next_id = 1
        # Startup state of every MCP server that has announced itself on this
        # connection, keyed by name. Only the connection that started a thread
        # hears these, which is why the bridge starts the thread itself
        # (docs/codex-mcp-readiness.md).
        self.mcp_startup = {}

    async def __aenter__(self):
        connector = aiohttp.UnixConnector(path=self.socket_path)
        self.session = aiohttp.ClientSession(connector=connector)
        try:
            self.websocket = await self.session.ws_connect("http://localhost/")
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "claude_code_tui_review_bridge",
                        "title": "Claude Code TUI Review Bridge",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            )
            await self.websocket.send_json({"method": "initialized", "params": {}})
        except Exception:
            await self.session.close()
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.websocket.close()
        await self.session.close()

    async def request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        await self.websocket.send_json(
            {"id": request_id, "method": method, "params": params}
        )

        while True:
            message = await self.websocket.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                raise AppServerError(
                    f"Unexpected app-server WebSocket message type: {message.type}"
                )

            payload = json.loads(message.data)
            if payload.get("id") == request_id:
                if payload.get("error"):
                    error = payload["error"]
                    raise AppServerError(
                        f"{method} failed: {error.get('message', error)}"
                    )
                return payload.get("result", {})

            await self._handle_unsolicited(payload)

    async def pump(self, seconds):
        """Reads whatever the server volunteers for a while; returns nothing.

        Notifications only arrive while someone is reading the socket, and the
        MCP readiness gate has no request of its own to wait on — so it needs a
        way to listen without asking anything.
        """
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                message = await asyncio.wait_for(
                    self.websocket.receive(), timeout=remaining
                )
            except asyncio.TimeoutError:
                return
            if message.type != aiohttp.WSMsgType.TEXT:
                raise AppServerError(
                    f"Unexpected app-server WebSocket message type: {message.type}"
                )
            await self._handle_unsolicited(json.loads(message.data))

    async def _handle_unsolicited(self, payload):
        """Records a notification, or declines a server request; returns nothing."""
        if payload.get("method") == MCP_STARTUP_NOTIFICATION:
            params = payload.get("params") or {}
            name = params.get("name")
            if name:
                self.mcp_startup[name] = params.get("status")
            return

        if payload.get("id") is not None and payload.get("method"):
            await self.websocket.send_json(
                {
                    "id": payload["id"],
                    "error": {
                        "code": -32601,
                        "message": (
                            "The read-only Claude bridge does not answer "
                            f"server request {payload['method']}."
                        ),
                    },
                }
            )


# The whole review request, stated here rather than delegated to a skill name:
# this reviewer is a different vendor's session, where a name from this side
# resolves to nothing. Same contract as the `rounds` block in
# dispatch/templates/shapes.toml, which the reviewed child carries in its first
# turn; the two copies are the two ends of one review.
ROUNDS_CONTRACT = (
    "Rounds contract. Classify each finding on two axes: standards — style, "
    "naming, convention, anything that leaves behaviour intact — and spec — "
    "correctness, security, deviation from the spec or ticket. The caller fixes "
    "the standards findings it accepts in one pass; spec findings that required "
    "fixes earn one re-review, scoped to exactly those fixes. A spec finding "
    "still open after that re-review, or a finding reopened once it was ruled "
    "on, ends the review: state the disagreement with both positions, which the "
    "caller escalates to its coordinator rather than opening another round."
)


def build_review_prompt(target, bridge_id):
    return (
        f"[claude-tui-review-bridge:{bridge_id}]\n"
        f"Review {target} and report your findings. You are in an interactive TUI "
        "with full local access. If the originating issue or spec cannot be "
        "fetched, treat it as no spec available: run the standards axis and "
        "report the spec axis as skipped. Report on the code as it stands; this "
        f"is a review-only task.\n\n{ROUNDS_CONTRACT}"
    )


def build_followup_prompt(target, bridge_id):
    return (
        f"[claude-tui-review-bridge:{bridge_id}]\n"
        f"Continue this review thread with the updated {target}. Compare it with "
        "the findings already in this thread, and say for each earlier finding "
        "whether it is now resolved. This is the one re-review the contract "
        "allows, so scope it to those fixes. Report on the code as it stands; "
        f"this is a review-only task.\n\n{ROUNDS_CONTRACT}"
    )


def build_prompt(args, bridge_id, followup=False):
    if args.browser_probe:
        return build_browser_probe_prompt(bridge_id)
    if args.probe:
        return build_probe_prompt(bridge_id)
    if followup:
        return build_followup_prompt(args.target, bridge_id)
    return build_review_prompt(args.target, bridge_id)


def build_probe_prompt(bridge_id):
    return (
        f"[claude-tui-review-bridge:{bridge_id}]\n"
        "This is a bridge health probe. Do not run commands, read files, or call "
        "tools. Reply with exactly: TUI_REVIEW_BRIDGE_OK"
    )


def build_browser_probe_prompt(bridge_id):
    return (
        f"[claude-tui-review-bridge:{bridge_id}]\n"
        "This is an authorized end-to-end browser-control probe. Use the installed "
        "Browser control skill and its browser runtime. Do not use curl, web search, "
        "Playwright CLI, or another HTTP client. Automatically open "
        "https://example.com in the runtime-selected browser, read the page title "
        "and visible h1 text, then close only the tab created for this probe. Report "
        "the selected browser backend, title, h1, and whether cleanup succeeded. If "
        "browser connection or control fails, report the exact failure without "
        "substituting another method."
    )


def read_log_tail(path, limit=4000):
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


def wait_for_path(path, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False


def terminate_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def child_session_env(hook):
    """Environment for the Codex processes this bridge spawns.

    A review child is a child launch like any other, so it carries whatever the
    project's on-child-launch hook adds — nothing, until a project configures it.
    """
    return hook.child_env()


def model_config_overrides(args):
    """`-c` overrides pinning model/effort, or nothing when neither was chosen.

    Both are optional everywhere they appear. When the caller names neither,
    this returns an empty list and Codex resolves model and reasoning effort
    from `~/.codex/config.toml` exactly as it did before these options existed.
    Effort has no dedicated CLI flag in codex-cli 0.147.0, so it can only be
    pinned through a config override.
    """
    overrides = []
    model = getattr(args, "model", None)
    effort = getattr(args, "effort", None)
    if model:
        overrides.extend(["-c", f"model={json.dumps(model)}"])
    if effort:
        overrides.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
    return overrides


def run_pane(args):
    runtime_dir = pathlib.Path(args.runtime_dir)
    socket_path = runtime_dir / "app-server.sock"
    log_path = runtime_dir / "app-server.log"
    log_file = log_path.open("a", encoding="utf-8")
    child_env = child_session_env(launch_hook.load_hook(args.cwd))
    app_server_command = [
        "codex",
        "app-server",
        "--listen",
        f"unix://{socket_path}",
    ]
    if args.network:
        app_server_command.extend(
            [
                "-c",
                "sandbox_workspace_write.network_access=true",
                "-c",
                'web_search="live"',
            ]
        )
    app_server_command.extend(model_config_overrides(args))
    app_server = subprocess.Popen(
        app_server_command,
        cwd=args.cwd,
        env=child_env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )

    def cleanup(*_ignored):
        terminate_process(app_server)
        log_file.close()
        shutil.rmtree(runtime_dir, ignore_errors=True)

    def stop_and_exit(signum, _frame):
        cleanup()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGHUP, stop_and_exit)
    signal.signal(signal.SIGTERM, stop_and_exit)
    signal.signal(signal.SIGINT, stop_and_exit)

    try:
        if not wait_for_path(socket_path, args.startup_timeout):
            detail = read_log_tail(log_path) or "app-server socket did not appear"
            print(f"Codex app-server failed to start: {detail}", file=sys.stderr)
            return 1

        # The parent starts the thread, waits for MCP, and submits the first
        # turn before naming the thread here. A parent that dies instead kills
        # this pane, so this bound is only a backstop.
        thread_id = wait_for_thread_handoff(runtime_dir, args.handoff_timeout)
        if not thread_id:
            print(
                "Timed out waiting for the bridge to hand over its Codex thread",
                file=sys.stderr,
            )
            return 1

        command = build_tui_command(args, socket_path, thread_id)
        return subprocess.run(
            command, cwd=args.cwd, env=child_env, check=False
        ).returncode
    finally:
        cleanup()


def build_tui_command(args, socket_path, thread_id):
    """Returns the TUI launch command, which attaches to a thread and carries no prompt.

    A positional prompt is submitted the moment the TUI starts, which is what
    used to race the session's MCP servers (issue #14). The turn is submitted
    over the app-server instead, once the readiness gate has opened.
    """
    command = [
        "codex",
        "--remote",
        f"unix://{socket_path}",
        "--sandbox",
        args.sandbox,
        "--ask-for-approval",
        args.approval,
    ]
    if args.network:
        command.extend(
            ["-c", "sandbox_workspace_write.network_access=true", "--search"]
        )
    command.extend(model_config_overrides(args))
    command.extend(["resume", thread_id])
    return command


def pane_exists(pane_id):
    # display-message -t on a dead pane exits 0 on tmux >= 3.6, so test
    # membership in the full pane list instead.
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and pane_id in result.stdout.split()


def close_pane(pane_id):
    subprocess.run(
        ["tmux", "kill-pane", "-t", pane_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def cleanup_failed_pane(pane_id, runtime_dir):
    close_pane(pane_id)
    deadline = time.monotonic() + 2
    while pane_exists(pane_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    shutil.rmtree(runtime_dir, ignore_errors=True)


def handoff_timeout(startup_timeout):
    """Returns how long the pane waits to be told which thread to attach to.

    Covers every step the parent takes before it hands over: connecting to the
    app-server and the readiness gate, each bounded by `startup_timeout`, plus
    the first turn in between.
    """
    return startup_timeout * 2 + HANDOFF_GRACE_SECONDS


def launch_pane(args, runtime_dir):
    if not args.tmux_target:
        raise RuntimeError("Missing originating tmux pane")
    pane_command = [
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "_pane",
        "--runtime-dir",
        str(runtime_dir),
        "--cwd",
        args.cwd,
        "--sandbox",
        args.sandbox,
        "--approval",
        args.approval,
        "--startup-timeout",
        str(args.startup_timeout),
        "--handoff-timeout",
        str(handoff_timeout(args.startup_timeout)),
    ]
    if args.network:
        pane_command.append("--network")
    if getattr(args, "model", None):
        pane_command.extend(["--model", args.model])
    if getattr(args, "effort", None):
        pane_command.extend(["--effort", args.effort])

    tmux_command = [
        "tmux",
        "split-window",
        "-h",
        "-P",
        "-F",
        "#{pane_id}",
        "-c",
        args.cwd,
    ]
    tmux_command.extend(["-t", args.tmux_target])
    tmux_command.append(shlex.join(pane_command))

    result = subprocess.run(
        tmux_command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "tmux split-window failed")
    pane_id = result.stdout.strip()
    launch_hook.load_hook(args.cwd).run(args.cwd, tmux_target=pane_id)
    return pane_id


def user_message_text(item):
    if item.get("type") != "userMessage":
        return ""
    parts = []
    for content in item.get("content") or []:
        if content.get("type") == "text":
            parts.append(content.get("text", ""))
    return "\n".join(parts)


def find_bridge_turn(thread, marker):
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            if marker in user_message_text(item):
                return turn
    return None


def final_agent_message(turn):
    messages = [
        item.get("text", "")
        for item in turn.get("items") or []
        if item.get("type") == "agentMessage"
        and item.get("phase") == "final_answer"
    ]
    if messages:
        return messages[-1]
    fallback = [
        item.get("text", "")
        for item in turn.get("items") or []
        if item.get("type") == "agentMessage"
    ]
    return fallback[-1] if fallback else ""


async def connect_when_ready(socket_path, pane_id, timeout_seconds, log_path):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        if not pane_exists(pane_id):
            detail = read_log_tail(log_path)
            raise RuntimeError(
                "Codex TUI pane exited during startup"
                + (f": {detail}" if detail else "")
            )
        if os.path.exists(socket_path):
            try:
                client = AppServerClient(socket_path)
                return await client.__aenter__()
            except (OSError, aiohttp.ClientError, AppServerError) as error:
                last_error = error
        await asyncio.sleep(0.1)
    detail = read_log_tail(log_path)
    suffix = detail or str(last_error or "socket unavailable")
    raise RuntimeError(f"Timed out connecting to Codex app-server: {suffix}")


async def start_thread(client, cwd, sandbox, approval):
    """Returns a new thread id with its unattended approval and sandbox policy.

    Thread start takes the sandbox as a bare enum string, unlike the tagged
    sandbox object used by turn start; the session approval policy suppresses
    per-tool prompts in this unattended review.
    """
    result = await client.request(
        "thread/start",
        {
            "cwd": cwd,
            "approvalPolicy": approval,
            "sandbox": sandbox,
        },
    )
    thread_id = (result.get("thread") or {}).get("id")
    if not thread_id:
        raise RuntimeError("Codex app-server started a thread without an id")
    return thread_id


async def configured_mcp_server_names(client):
    """Returns the names of every MCP server Codex is configured with."""
    result = await client.request(
        "mcpServerStatus/list", {"detail": "toolsAndAuthOnly"}
    )
    return {
        entry.get("name") for entry in result.get("data") or [] if entry.get("name")
    }


def unsettled_mcp_report(announced, configured):
    still_starting = sorted(
        name
        for name, status in announced.items()
        if status not in MCP_STARTUP_SETTLED_STATES
    )
    if still_starting:
        return "still starting: " + ", ".join(still_starting)
    return "none of " + ", ".join(sorted(configured)) + " announced itself"


async def wait_for_mcp_startup(client, pane_id, timeout_seconds):
    """Returns the settled startup state of this thread's MCP servers.

    Blocks until they have finished coming up, and raises if they do not.

    Waits on the announcements rather than on a clock: the gate opens once every
    server that has announced itself has left `starting`. Servers that never
    announce are not waited for — the configured inventory routinely lists one
    that never starts, so requiring the whole inventory would hang forever
    (docs/codex-mcp-readiness.md).

    Because a late announcement would otherwise slip through that rule, the gate
    only trusts a settled set once either every configured server has been heard
    from, or nothing new has arrived for `MCP_STARTUP_QUIET_SECONDS`.
    """
    # The inventory is an RPC of its own, measured at ~1.5 s, and nothing under
    # it ever times out: awaited plainly, a stuck app-server would hang here
    # past every budget the caller set. It gets the deadline's remaining time.
    deadline = time.monotonic() + timeout_seconds
    try:
        configured = await asyncio.wait_for(
            configured_mcp_server_names(client),
            timeout=max(0.0, deadline - time.monotonic()),
        )
    except asyncio.TimeoutError as error:
        raise RuntimeError(
            "Timed out asking Codex which MCP servers are configured"
        ) from error
    if not configured:
        return {}

    seen = None
    settled_since = None
    while time.monotonic() < deadline:
        if pane_id is not None and not pane_exists(pane_id):
            raise RuntimeError(
                "Codex TUI pane exited before its MCP servers finished starting"
            )

        announced = dict(client.mcp_startup)
        if announced != seen:
            # Something changed, so any quiet already counted no longer counts.
            seen = announced
            settled_since = None

        if announced and all(
            status in MCP_STARTUP_SETTLED_STATES for status in announced.values()
        ):
            if configured <= announced.keys():
                return announced
            now = time.monotonic()
            if settled_since is None:
                settled_since = now
            elif now - settled_since >= MCP_STARTUP_QUIET_SECONDS:
                return announced

        await client.pump(0.1)

    raise RuntimeError(
        "Timed out waiting for Codex MCP servers to start ("
        + unsettled_mcp_report(dict(client.mcp_startup), configured)
        + ")"
    )


def hand_off_thread(runtime_dir, thread_id):
    """Tells the waiting pane which thread to attach its TUI to; returns nothing.

    Written only after the first turn has started, because `resume` refuses a
    thread that has no rollout yet.
    """
    path = pathlib.Path(runtime_dir) / THREAD_HANDOFF_FILENAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(thread_id, encoding="utf-8")
    os.replace(temporary, path)


def wait_for_thread_handoff(runtime_dir, timeout_seconds):
    """Returns the thread id the parent hands over, or None if it never arrives."""
    path = pathlib.Path(runtime_dir) / THREAD_HANDOFF_FILENAME
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            thread_id = path.read_text(encoding="utf-8").strip()
        except OSError:
            thread_id = ""
        if thread_id:
            return thread_id
        time.sleep(0.05)
    return None


async def wait_for_review(client, thread_id, marker, pane_id, timeout_seconds):
    """Returns the (thread, turn) pair once the review turn reaches a terminal status."""
    deadline = time.monotonic() + timeout_seconds
    unreadable = None
    while time.monotonic() < deadline:
        try:
            result = await client.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
        except AppServerError as error:
            # A turn that has only just been submitted has not necessarily
            # flushed its rollout yet, and the thread cannot be read until it
            # has. That is this poll's normal first answer, not a failure.
            unreadable = error
            result = None

        if result is not None:
            thread = result.get("thread") or {}
            turn = find_bridge_turn(thread, marker)
            if turn and turn.get("status") in TERMINAL_TURN_STATUSES:
                return thread, turn

        if not pane_exists(pane_id):
            raise RuntimeError("Codex TUI pane exited before the review turn completed")
        await asyncio.sleep(0.5)

    raise RuntimeError(
        "Timed out waiting for the Codex review turn"
        + (f"; the thread never became readable: {unreadable}" if unreadable else "")
    )


async def start_turn(client, thread_id, prompt, model=None, effort=None):
    # `turn/start` accepts optional `model`/`effort` overrides that apply to
    # this turn and the ones after it. Both are omitted unless the lineage was
    # pinned, so an unpinned session keeps running on the thread's own model.
    params = {
        "threadId": thread_id,
        "input": [
            {
                "type": "text",
                "text": prompt,
                "text_elements": [],
            }
        ],
    }
    if model:
        params["model"] = model
    if effort:
        params["effort"] = effort
    return await client.request("turn/start", params)


async def start_followup_turn(client, state, prompt):
    return await start_turn(
        client, state["threadId"], prompt, state.get("model"), state.get("effort")
    )


def should_cleanup_session(turn, final_message):
    status = turn.get("status")
    return status == "failed" or (status == "completed" and not final_message)


def make_runtime(prompt):
    """Returns the session's runtime directory, the request recorded beside its log.

    Nothing reads prompt.txt back — the turn is submitted over the app-server —
    but it keeps what was asked next to app-server.log when a run needs
    explaining afterwards.
    """
    runtime_dir = pathlib.Path(tempfile.mkdtemp(prefix="claude-codex-tui-"))
    (runtime_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    return runtime_dir


def session_state(
    session_id, owner, runtime_dir, pane_id, thread_id, target, model, effort,
    marker,
):
    now = time.time()
    return {
        "version": SESSION_STATE_VERSION,
        "reviewSessionId": session_id,
        "owner": owner.to_dict(),
        "runtimeDir": str(runtime_dir),
        "socketPath": str(runtime_dir / "app-server.sock"),
        "paneId": pane_id,
        "threadId": thread_id,
        "target": target,
        "model": model,
        "effort": effort,
        # The marker of the turn now in flight: the handle a recovering caller
        # needs to find that turn again in the thread.
        "marker": marker,
        "createdAt": now,
        "updatedAt": now,
    }


def apply_session_model_choice(args, state):
    """Reconcile a follow-up's model/effort with the ones the lineage carries.

    A follow-up that names neither inherits whatever the first review pinned,
    so the whole lineage keeps one model and one effort. Naming either one
    repins the lineage from this turn on. A lineage that was never pinned
    stays unpinned, which is the pre-existing behaviour.
    """
    if args.model:
        state["model"] = args.model
    else:
        args.model = state.get("model")
    if args.effort:
        state["effort"] = args.effort
    else:
        args.effort = state.get("effort")


def update_session_after_turn(state, turn, target):
    state["target"] = target
    state["lastTurnId"] = turn.get("id")
    state["lastStatus"] = turn.get("status")
    state["updatedAt"] = time.time()


def cleanup_session(state, store):
    cleanup_failed_pane(state.get("paneId"), state.get("runtimeDir"))
    store.delete(state["reviewSessionId"])


async def run_new_review(args, owner, store):
    bridge_id = str(uuid.uuid4())
    marker = f"[claude-tui-review-bridge:{bridge_id}]"
    prompt = build_prompt(args, bridge_id)

    runtime_dir = make_runtime(prompt)
    args.tmux_target = owner.origin_pane
    try:
        pane_id = launch_pane(args, runtime_dir)
    except Exception:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        raise

    state = None
    try:
        client = await connect_when_ready(
            runtime_dir / "app-server.sock",
            pane_id,
            args.startup_timeout,
            runtime_dir / "app-server.log",
        )
        try:
            thread_id = await start_thread(
                client, cwd=args.cwd, sandbox=args.sandbox, approval=args.approval
            )
            await wait_for_mcp_startup(client, pane_id, args.startup_timeout)
            await start_turn(client, thread_id, prompt, args.model, args.effort)
            session_id = str(uuid.uuid4())
            state = session_state(
                session_id,
                owner,
                runtime_dir,
                pane_id,
                thread_id,
                args.target,
                args.model,
                args.effort,
                marker,
            )
            # The record goes down before the pane is told to attach. Handing
            # over first would let a driver killed in between leave a live pane
            # running a review that `--recover-session` can no longer find.
            store.write(session_id, state)
            # Only now does the thread have a rollout for the TUI to resume.
            hand_off_thread(runtime_dir, thread_id)
            thread, turn = await wait_for_review(
                client, thread_id, marker, pane_id, args.timeout
            )
        finally:
            await client.__aexit__(None, None, None)
    except Exception:
        if state is None:
            cleanup_failed_pane(pane_id, runtime_dir)
        raise
    return state, thread, turn, False


async def connect_existing_session(state):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not pane_exists(state["paneId"]) or not os.path.exists(
            state["socketPath"]
        ):
            return None
        client = AppServerClient(state["socketPath"])
        try:
            return await client.__aenter__()
        except (OSError, aiohttp.ClientError, AppServerError):
            await asyncio.sleep(0.1)
    return None


async def resume_session_in_new_pane(args, owner, store, state, prompt, marker):
    close_pane(state["paneId"])
    shutil.rmtree(state["runtimeDir"], ignore_errors=True)

    runtime_dir = make_runtime(prompt)
    args.tmux_target = owner.origin_pane
    try:
        pane_id = launch_pane(args, runtime_dir)
    except Exception:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        raise

    try:
        client = await connect_when_ready(
            runtime_dir / "app-server.sock",
            pane_id,
            args.startup_timeout,
            runtime_dir / "app-server.log",
        )
        # This app-server is as cold as a new review's, so the thread has to be
        # reopened here — both to boot its MCP servers and to hear them announce
        # — before the follow-up turn may go in.
        await client.request("thread/resume", {"threadId": state["threadId"]})
        await wait_for_mcp_startup(client, pane_id, args.startup_timeout)
        await start_followup_turn(client, state, prompt)
        # The record names the new pane before that pane is told to attach, so a
        # driver killed in between still leaves a recoverable review.
        state["runtimeDir"] = str(runtime_dir)
        state["socketPath"] = str(runtime_dir / "app-server.sock")
        state["paneId"] = pane_id
        state["updatedAt"] = time.time()
        store.write(state["reviewSessionId"], state)
        hand_off_thread(runtime_dir, state["threadId"])
    except Exception:
        cleanup_failed_pane(pane_id, runtime_dir)
        raise

    try:
        return await wait_for_review(
            client,
            state["threadId"],
            marker,
            pane_id,
            args.timeout,
        )
    finally:
        await client.__aexit__(None, None, None)


async def run_existing_review(args, owner, store):
    state = store.read(args.resume_session)
    validate_session_owner(state, owner)
    apply_session_model_choice(args, state)
    bridge_id = str(uuid.uuid4())
    marker = f"[claude-tui-review-bridge:{bridge_id}]"
    prompt = build_prompt(args, bridge_id, followup=True)
    # On disk before the turn is awaited, for the same reason the first review
    # writes its record early: a driver killed mid-turn must leave a marker the
    # recovery path can wait on.
    state["marker"] = marker

    client = await connect_existing_session(state)
    if client is None:
        thread, turn = await resume_session_in_new_pane(
            args, owner, store, state, prompt, marker
        )
    else:
        try:
            store.write(state["reviewSessionId"], state)
            await start_followup_turn(client, state, prompt)
            thread, turn = await wait_for_review(
                client,
                state["threadId"],
                marker,
                state["paneId"],
                args.timeout,
            )
        finally:
            await client.__aexit__(None, None, None)
    return state, thread, turn, True


async def run_recovered_review(args, owner, store):
    """Re-attach to the review this owner already has running.

    Returns the same `(state, thread, turn, reused)` tuple the other two review
    paths return, for the session this owner already owns.

    The caller reaching here has lost the handle its driver was going to print —
    the driver was killed, or its output never arrived. Everything needed to pick
    the review back up survived that death: the record on disk, the pane, and the
    thread. So this starts no pane and no thread; it finds the owner's live
    session and waits on the turn already in flight. With no such session it
    raises `NoLiveSessionError` rather than falling back to a first review.
    """
    for state in store.find_by_owner(owner):
        client = await connect_existing_session(state)
        if client is None:
            continue
        # The record's own target, so the turn that is already in flight is
        # reported as what it actually reviews.
        args.target = state["target"]
        try:
            thread, turn = await wait_for_review(
                client,
                state["threadId"],
                state["marker"],
                state["paneId"],
                args.timeout,
            )
        finally:
            await client.__aexit__(None, None, None)
        return state, thread, turn, True
    raise NoLiveSessionError(
        "No live review session for this tmux pane and worktree. "
        "Nothing to recover; start a review instead."
    )


async def run_bridge(args):
    if not pathlib.Path(args.cwd).is_dir():
        raise RuntimeError(f"Working directory does not exist: {args.cwd}")
    owner = resolve_owner(args)
    store = SessionStore()
    # The lock stays process-scoped: it serialises concurrent calls from one
    # pane, and a driver that dies releases it. Duplicate prevention across a
    # driver's death is the recovery path's job, not a longer-lived lock's — a
    # lock that outlived its holder would also have to be reaped, and would
    # block the very recovery call that clears the duplicate.
    with owner_lock(store, owner):
        if args.recover_session:
            state, thread, turn, reused = await run_recovered_review(
                args, owner, store
            )
        elif args.resume_session:
            state, thread, turn, reused = await run_existing_review(
                args, owner, store
            )
        else:
            state, thread, turn, reused = await run_new_review(
                args, owner, store
            )
        final_message = final_agent_message(turn)
        update_session_after_turn(state, turn, args.target)
        store.write(state["reviewSessionId"], state)
        cleanup_required = should_cleanup_session(turn, final_message)
        if cleanup_required:
            cleanup_session(state, store)
    output = {
        "status": turn.get("status"),
        "reviewSessionId": state["reviewSessionId"],
        "reused": reused,
        "recovered": bool(args.recover_session),
        "threadId": thread.get("id"),
        "turnId": turn.get("id"),
        "paneId": state["paneId"],
        "finalMessage": final_message,
    }
    print(json.dumps(output, ensure_ascii=False))
    succeeded = turn.get("status") == "completed" and bool(output["finalMessage"])
    return 0 if succeeded else 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Launch or resume an interactive Codex TUI review."
    )
    parser.add_argument("target", nargs="?")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT_SECONDS
    )
    parser.add_argument("--sandbox", default="danger-full-access")
    parser.add_argument("--approval", default="never")
    parser.add_argument(
        "--model", help="Codex model for this review lineage (default: Codex config)"
    )
    parser.add_argument(
        "--effort",
        help="Reasoning effort for this review lineage (default: Codex config)",
    )
    parser.add_argument("--network", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--resume-session")
    parser.add_argument(
        "--recover-session",
        action="store_true",
        help=(
            "re-attach to the live review this tmux pane and worktree already "
            f"own, instead of starting one (exit {NO_LIVE_SESSION_EXIT} when "
            "there is none)"
        ),
    )
    parser.add_argument(
        "--machine-log",
        help="the run's machine log, where this review's `review` event pair is "
             "appended; the pair is written only when this and --ticket are "
             "both given",
    )
    parser.add_argument(
        "--ticket",
        help="the ticket this review is for, as the machine log spells it; the "
             "review's event pair is written only when this and --machine-log "
             "are both given",
    )
    parser.add_argument("--tmux-target", help=argparse.SUPPRESS)
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--browser-probe", action="store_true", help=argparse.SUPPRESS)
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.recover_session:
        # Recovery names no target and no session: the owner tuple is the
        # lookup key, and the target comes from the record it finds.
        if args.target:
            parser.error("--recover-session takes no target")
        if args.resume_session:
            parser.error("--recover-session and --resume-session are exclusive")
    elif not args.target:
        parser.error("the target to review is required")
    return args


def build_pane_parser():
    pane_parser = argparse.ArgumentParser(add_help=False)
    pane_parser.add_argument("--runtime-dir", required=True)
    pane_parser.add_argument("--cwd", required=True)
    pane_parser.add_argument("--sandbox", required=True)
    pane_parser.add_argument("--approval", required=True)
    pane_parser.add_argument("--startup-timeout", type=float, required=True)
    pane_parser.add_argument("--handoff-timeout", type=float, required=True)
    pane_parser.add_argument("--network", action="store_true")
    pane_parser.add_argument("--model")
    pane_parser.add_argument("--effort")
    return pane_parser


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_pane":
        args = build_pane_parser().parse_args(sys.argv[2:])
        return run_pane(args)
    args = parse_args()
    # The pair straddles the whole call, so the `returned` line is written on
    # every exit path this bridge controls — a review that failed, timed out, or
    # raised included. A row claiming a review that is no longer running is worse
    # than no row at all.
    review = ReviewEvent(args.machine_log, args.ticket, args.model)
    review.write("running")
    try:
        return asyncio.run(run_bridge(args))
    except NoLiveSessionError as error:
        print(str(error), file=sys.stderr)
        return NO_LIVE_SESSION_EXIT
    except (AppServerError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        review.write("returned")


if __name__ == "__main__":
    raise SystemExit(main())
