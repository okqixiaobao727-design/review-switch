#!/usr/bin/env python3

"""Interactive Codex TUI code-review channel for a Claude session.

Round one starts a review lineage in its own tmux pane; round two resumes that
thread, and `--recover-session` re-attaches to the turn a killed driver left in
flight.

Preparation and delivery are separate: preparation fills one Axis Brief per
requested axis, and the Lane `--reviewer` names takes one brief and gives back
that axis's result. A reviewer this bridge has no Lane for is refused by name
before any of it opens.

A caller that wants a review's start, each axis's cost, and its end observed
hands in the commands to run at those points, and gets them run with this
review's own facts in their environment. Pass none and nothing extra runs. The
whole contact surface is this command, its result, and those hooks: nothing here
reads a caller's configuration or names a caller's vocabulary (ADR-0002).
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
SESSION_STATE_VERSION = 2
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Recovery found nothing to re-attach to, which is a different answer from a
# failed review: it is the one result that licenses starting a first review.
NO_LIVE_SESSION_EXIT = 3
# The reviewing vendor a caller names on `--reviewer`. The model is whatever the
# lineage was pinned to, so the reviewer names the vendor and nothing else.
REVIEWER_CODEX = "codex"
CODE_GRAPH_CLI = "code-review-graph"
# The four disjoint counters one axis's spend is reported as.
COUNTERS = ("input", "output", "cache_read", "cache_creation")
# The four points in a review's life a caller may hand one command for, each
# mapped to when its command fires. The point's own name is both the option it
# arrives on and the `REVIEW_EVENT` its command is handed, so a caller reading
# either knows the other.
CHILD_LAUNCH = "child-launch"
REVIEW_START = "review-start"
AXIS_END = "axis-end"
REVIEW_END = "review-end"
HOOK_POINTS = {
    CHILD_LAUNCH: "once per launched reviewer process or pane",
    REVIEW_START: (
        "once per invocation, after preparation succeeds and before any Lane opens"
    ),
    AXIS_END: "once per started axis, on every exit path that axis can take",
    REVIEW_END: "once per invocation, on every exit path",
}
HOOK_TIMEOUT_SECONDS = 30
EVENT_VAR = "REVIEW_EVENT"
COST_DETAIL_VAR = "REVIEW_COST_DETAIL"
# One environment variable per counter, spelled from the counter itself so the
# two cannot drift apart.
COUNTER_VARS = {name: f"REVIEW_{name.upper()}_TOKENS" for name in COUNTERS}
# Every variable some point sets. All of them leave the inherited environment
# before this review's own facts go in, so a review run from inside another
# review's hook cannot pass that review's facts off as this one's — the counters
# and `REVIEW_COST_DETAIL` above all, which are answers to the same question and
# never both true. A caller's own variables, this prefix included, are untouched.
HOOK_VARS = frozenset({
    EVENT_VAR,
    COST_DETAIL_VAR,
    "REVIEW_CHILD_CWD",
    "REVIEW_CHILD_TMUX_TARGET",
    "REVIEW_REVIEWER",
    "REVIEW_MODEL",
    "REVIEW_AXES",
    "REVIEW_AXIS",
    "REVIEW_STATUS",
    "REVIEW_SESSION",
    *COUNTER_VARS.values(),
})
# What a review that never reached a result of its own is: the result contract's
# spelling for a failure, which is what both status-carrying points use.
FAILED_STATUS = "failed"
NO_THREAD_DETAIL = "this axis started no thread to read a cost from"
# Where Codex keeps the rollout of every thread it has run, one JSONL file per
# thread under a dated tree, each filename ending in the thread's own id.
CODEX_HOME_ENV_VAR = "CODEX_HOME"
CODEX_HOME_DEFAULT = ".codex"
ROLLOUT_DIRECTORY = "sessions"
ROLLOUT_GLOB = "**/*.jsonl"
# The counters a rollout's cumulative `total_token_usage` must carry for it to be
# read at all. `reasoning_output_tokens` is not among them: it counts a subset of
# the output tokens rather than a fifth pot, and nothing here needs it.
# A `token_count` event that carried no usage object: held apart from "no count at all", because
# what it means is that this thread's last word on what it spent is unreadable.
MALFORMED_COUNT = object()
ROLLOUT_USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "total_tokens",
)


class AppServerError(RuntimeError):
    pass


class NoLiveSessionError(RuntimeError):
    """No live review session belongs to the caller's owner identity."""


def hook_command(args, point):
    """The command this caller handed in for one point, or empty where it handed none."""
    return getattr(args, hook_destination(point), None) or ""


def hook_destination(point):
    """The attribute one point's command arrives under, from the point's own name."""
    return "on_" + point.replace("-", "_")


def run_hook(args, point, **facts):
    """Run the caller's command for one point of this review, if it handed one in.

    The command is the caller's own, so it may carry whatever correlation token
    that caller needs; all this adds is facts this review owns, in the
    environment, plus the point's name in `REVIEW_EVENT`. It runs once, in the
    reviewed working directory, with its output discarded and a bound on how long
    it may take.

    Observation must never change what a review returns, so a command that fails,
    times out, or is not installed is swallowed here: the caller's exit status and
    the JSON object it reads are the same either way.
    """
    command = hook_command(args, point)
    if not command:
        return
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in HOOK_VARS
    }
    environment[EVENT_VAR] = point
    environment.update(facts)
    try:
        subprocess.run(
            command,
            shell=True,
            cwd=args.cwd,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=HOOK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def hook_child_launch(args, tmux_target=""):
    """Name the reviewer child that just launched, and the window it runs in."""
    run_hook(
        args,
        CHILD_LAUNCH,
        REVIEW_CHILD_CWD=str(args.cwd),
        REVIEW_CHILD_TMUX_TARGET=tmux_target,
    )


def hook_review_start(args, model):
    """Name the review about to open: its reviewer, its model, and its axes."""
    run_hook(
        args,
        REVIEW_START,
        REVIEW_REVIEWER=args.reviewer,
        REVIEW_MODEL=model or "",
        REVIEW_AXES=",".join(requested_axes(args)),
    )


def cost_facts(counters, detail):
    """One axis's spend: the four disjoint counters, or why there are none.

    A figure invented out of a contradiction is worse than the diagnosis that
    says the rollout could not be read, so the two never arrive together.
    """
    if counters is None:
        return {COST_DETAIL_VAR: detail or ""}
    return {COUNTER_VARS[name]: str(counters[name]) for name in COUNTERS}


def hook_axis_end(args, axis, status, session, thread_id):
    """End one axis: how it finished, and what its own thread spent.

    The model reported is the one the thread resolved to and nothing else: the
    alias the caller asked for is already theirs to remember. An axis that never
    started a thread has no rollout to read, and says so.
    """
    if thread_id is None:
        counters, model, detail = None, None, NO_THREAD_DETAIL
    else:
        counters, model, detail = harvest_rollout(codex_sessions_root(), thread_id)
    run_hook(
        args,
        AXIS_END,
        REVIEW_AXIS=axis,
        REVIEW_STATUS=status,
        REVIEW_SESSION=session or "",
        REVIEW_MODEL=model or "",
        **cost_facts(counters, detail),
    )


def end_launched_axis(args, axis):
    """End an axis that opened its pane and never got a result of its own.

    A sibling axis failing to launch tears this one down before it could report
    anything, and the point still owes the caller one end for the child it was
    told about.
    """
    hook_axis_end(args, axis, FAILED_STATUS, None, None)


def hook_review_end(args, status):
    """Close this review out, whichever way it left."""
    run_hook(args, REVIEW_END, REVIEW_STATUS=status)


def codex_sessions_root(environment=None):
    """The directory Codex writes its rollouts under, wherever this machine keeps it."""
    environment = os.environ if environment is None else environment
    home = environment.get(CODEX_HOME_ENV_VAR)
    root = pathlib.Path(home) if home else pathlib.Path.home() / CODEX_HOME_DEFAULT
    return root / ROLLOUT_DIRECTORY


def find_rollout(root, thread_id):
    """The rollout file this thread wrote, or `None` where the tree holds none.

    The id glob is the lookup: Codex names every rollout for the thread it
    belongs to, so the filename ending in the id the bridge already holds is the
    file, without reading a line of any other.
    """
    try:
        found = sorted(
            path for path in root.glob(ROLLOUT_GLOB) if path.stem.endswith(thread_id)
        )
    except OSError:
        return None
    return found[-1] if found else None


def rollout_counters(usage):
    """Codex's cumulative counters as the four disjoint ones an axis ends with, or `None`.

    Codex counts its cached tokens inside `input_tokens`, so they come back out
    here, and its reasoning tokens inside `output_tokens`, where they stay: the
    four that come out are disjoint, and their sum is the total Codex itself
    reported.

    `None` is every way the source does not hold together — a counter missing,
    not a count, or negative; more cached tokens than input tokens; a mapping
    that does not add up to the reported total. A figure invented out of a
    contradiction is worse than the diagnosis that says the rollout could not be
    read, because nothing downstream can tell it apart from a measurement.
    """
    counts = {}
    for name in ROLLOUT_USAGE_FIELDS:
        value = usage.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        counts[name] = value
    cached = counts["cached_input_tokens"]
    if cached > counts["input_tokens"]:
        return None
    counters = {
        "input": counts["input_tokens"] - cached,
        "output": counts["output_tokens"],
        "cache_read": cached,
        "cache_creation": counts["cache_write_input_tokens"],
    }
    if sum(counters.values()) != counts["total_tokens"]:
        return None
    return counters


def harvest_rollout(root, thread_id):
    """What one review thread spent: `(counters, model, detail)`, read off its rollout.

    A read of a file the lane already wrote, so it costs no model token and drives
    nothing. `counters` is `None` when the figures could not be read, and `detail`
    then says why, which is the diagnosis the axis ends with instead.

    The **last** cumulative count is the answer: every turn appends one, and a
    resumed round two appends to the same file, so one read covers a whole review
    however many rounds it had.

    Only the file's last line is forgiven for not parsing: the pane this reads
    behind is still attached, so that one may be half written and is unbilled
    either way. A line that does not parse with more of the rollout written after
    it is a hole in the history, and is diagnosed rather than quietly skipped.
    """
    path = find_rollout(root, thread_id)
    if path is None:
        return None, None, f"no Codex rollout under {root} ends in {thread_id}"
    reported = None
    model = None
    unparsed_last = False
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if unparsed_last:
                    return None, model, f"{path} carries a line that does not parse"
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    unparsed_last = True
                    continue
                if not isinstance(record, dict):
                    unparsed_last = True
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "turn_context" and payload.get("model"):
                    model = payload["model"]
                if payload.get("type") == "token_count":
                    info = payload.get("info")
                    usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    # The count this event carries, whatever shape it is in: a `token_count`
                    # whose usage is missing or is not an object is not an event to skip back
                    # past, because the count before it is no longer this thread's last word.
                    reported = usage if isinstance(usage, dict) else MALFORMED_COUNT
    except OSError as error:
        return None, model, f"{path} could not be read: {error.strerror}"
    if reported is None:
        return None, model, f"{path} reports no token count"
    if reported is MALFORMED_COUNT:
        return None, model, f"{path}'s last token count reports no usage"
    counters = rollout_counters(reported)
    if counters is None:
        return None, model, f"{path} reports a token count that does not hold together"
    return counters, model, None


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


def run_git(cwd, *arguments):
    return subprocess.run(
        ["git", "-C", cwd, *arguments],
        text=True,
        capture_output=True,
        check=False,
    )


def canonical_worktree_root(cwd):
    result = run_git(cwd, "rev-parse", "--show-toplevel")
    if result.returncode == 0 and result.stdout.strip():
        return str(pathlib.Path(result.stdout.strip()).resolve())
    return str(pathlib.Path(cwd).resolve())


def resolve_fixed_point(cwd, fixed_point):
    result = run_git(
        cwd, "rev-parse", "--verify", f"{fixed_point}^{{commit}}"
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"fixed point did not resolve: {fixed_point}")
    return result.stdout.strip()


def ensure_nonempty_diff(cwd, fixed_point):
    result = run_git(
        cwd, "diff", "--quiet", f"{fixed_point}...HEAD", "--"
    )
    if result.returncode == 0:
        raise RuntimeError(
            f"three-dot diff is empty: git diff {fixed_point}...HEAD"
        )
    if result.returncode != 1:
        detail = result.stderr.strip() or "git diff failed"
        raise RuntimeError(
            f"three-dot diff could not be read for {fixed_point}: {detail}"
        )


def resolve_main_checkout(cwd):
    result = run_git(
        cwd,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return str(pathlib.Path(result.stdout.strip()).parent)


def has_uncommitted_changes(cwd):
    result = run_git(cwd, "status", "--porcelain")
    return result.returncode != 0 or bool(result.stdout.strip())


def review_graph_base(cwd, main_checkout, fixed_point):
    if pathlib.Path(cwd).resolve() == pathlib.Path(main_checkout).resolve():
        return fixed_point
    result = run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    tip = result.stdout.strip()
    if tip == "HEAD":
        result = run_git(cwd, "rev-parse", "HEAD")
        if result.returncode != 0 or not result.stdout.strip():
            return None
        tip = result.stdout.strip()
    return f"{fixed_point}...{tip}"


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


STANDARDS_BRIEF_TEMPLATE = """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff {fixed_point}...HEAD
Commits:
{commit_list}
Standards sources: {standards_files}

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

Report, per file/hunk where relevant, (a) every place the diff violates a documented standard: cite the standard (file + the rule); and (b) any baseline smell you spot: name it and quote the hunk. Distinguish hard violations from judgement calls: documented-standard breaches can be hard, but baseline smells are always judgement calls, and a documented repo standard overrides the baseline. Skip anything tooling enforces. Under 400 words."""


def with_trailing_newline(text):
    return text if text.endswith("\n") else f"{text}\n"


def build_navigation_block(graph_result, repo_root):
    root = pathlib.Path(repo_root).resolve()

    def navigation_fields(node):
        path = pathlib.Path(node["file_path"])
        if path.is_absolute():
            path = path.resolve().relative_to(root)
        return (
            path.as_posix(),
            node["line_start"],
            node["line_end"],
            node["name"],
        )

    priorities = graph_result["review_priorities"]
    priority_fields = {navigation_fields(node) for node in priorities}
    ordered = priorities + [
        node
        for node in graph_result["changed_functions"]
        if navigation_fields(node) not in priority_fields
    ]
    lines = []
    for node in ordered:
        path, line_start, line_end, name = navigation_fields(node)
        lines.append(f"{path}:{line_start}–{line_end}  {name}")
    return "\n".join(lines) or None


def append_navigation_block(brief, navigation_block):
    if navigation_block is None:
        return brief
    return (
        f"{brief}\n\n"
        "Start here (from the code graph; the diff is the full scope):\n"
        f"{navigation_block}"
    )


def read_code_graph_navigation(cwd, fixed_point):
    if has_uncommitted_changes(cwd):
        return None
    main_checkout = resolve_main_checkout(cwd)
    if main_checkout is None:
        return None
    executable = shutil.which(CODE_GRAPH_CLI)
    if executable is None:
        return None
    graph_base = review_graph_base(cwd, main_checkout, fixed_point)
    if graph_base is None:
        return None

    common_arguments = ["--base", graph_base, "--repo", main_checkout]
    try:
        status = subprocess.run(
            [executable, "status", "--json", "--repo", main_checkout],
            cwd=main_checkout,
            text=True,
            capture_output=True,
            check=False,
        )
        if status.returncode != 0:
            return None
        status_result = json.loads(status.stdout)
        if not isinstance(status_result, dict):
            return None
        graph_exists = any(
            status_result.get(field)
            for field in (
                "nodes",
                "files",
                "last_updated",
                "built_on_branch",
                "built_at_commit",
            )
        )
        if not graph_exists:
            return None
        update = subprocess.run(
            [executable, "update", "--brief", *common_arguments],
            cwd=main_checkout,
            text=True,
            capture_output=True,
            check=False,
        )
        if update.returncode != 0:
            return None
        detection = subprocess.run(
            [executable, "detect-changes", *common_arguments],
            cwd=main_checkout,
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, TypeError, ValueError):
        return None
    if detection.returncode != 0:
        return None
    try:
        result = json.loads(detection.stdout)
        return build_navigation_block(result, main_checkout)
    except (KeyError, TypeError, ValueError):
        return None


def build_standards_brief(fixed_point, commit_list, standards_files):
    return STANDARDS_BRIEF_TEMPLATE.format(
        fixed_point=fixed_point,
        commit_list=with_trailing_newline(commit_list),
        standards_files=(
            ", ".join(standards_files)
            if standards_files
            else "none documented; baseline only"
        ),
    )


SPEC_BRIEF_TEMPLATE = """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: git diff {fixed_point}...HEAD
Commits:
{commit_list}
Spec:
{spec_contents}
Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words."""


def build_spec_brief(fixed_point, commit_list, spec_contents):
    return SPEC_BRIEF_TEMPLATE.format(
        fixed_point=fixed_point,
        commit_list=with_trailing_newline(commit_list),
        spec_contents=with_trailing_newline(spec_contents),
    )


@dataclasses.dataclass(frozen=True)
class ReviewPreparation:
    fixed_point: str
    resolved_fixed_point: str
    commit_list: str
    spec_source: str
    spec_contents: str | None
    standards_files: tuple[str, ...]
    navigation_block: str | None = None
    code_graph_used: bool = False

    def brief(self, axis):
        """This axis's Axis Brief, ready for whichever Lane delivers it."""
        return AxisBrief(axis=axis, text=self.brief_text(axis))

    def briefs(self, axes):
        """One Axis Brief per axis, in the order a review runs them."""
        return tuple(self.brief(axis) for axis in axes)

    def brief_text(self, axis):
        """The text of one axis's brief, or the failure that there is none."""
        if axis == "standards":
            return append_navigation_block(
                build_standards_brief(
                    self.fixed_point, self.commit_list, self.standards_files
                ),
                self.navigation_block,
            )
        if axis == "spec":
            if self.spec_contents is None:
                raise RuntimeError(
                    "spec source was not provided; run with --axis standards "
                    "when no spec exists"
                )
            return append_navigation_block(
                build_spec_brief(
                    self.fixed_point, self.commit_list, self.spec_contents
                ),
                self.navigation_block,
            )
        raise RuntimeError("axis 'both' requires the two-pane fan-out")

    def report(self):
        return {
            "fixedPoint": self.resolved_fixed_point,
            "specSource": self.spec_source,
            "standardsFiles": list(self.standards_files),
            "codeGraphUsed": self.code_graph_used,
        }


@dataclasses.dataclass(frozen=True)
class AxisBrief:
    """The prompt one Lane receives for one axis.

    Preparation fills a brief and delivery reads one; a Lane changes who reads a
    brief, never what it says, which is what makes a finding on one Lane a
    finding on the other (ADR-0003).
    """

    axis: str
    text: str


def read_commit_list(cwd, fixed_point):
    result = run_git(cwd, "log", f"{fixed_point}..HEAD", "--oneline")
    if result.returncode != 0:
        detail = result.stderr.strip() or "git log failed"
        raise RuntimeError(f"commit list could not be read: {detail}")
    return result.stdout


def find_standards_files(repo_root):
    root = pathlib.Path(repo_root)
    documented = [
        path
        for path in (
            "CODING_STANDARDS.md",
            "CONTRIBUTING.md",
            "AGENTS.md",
            "CLAUDE.md",
        )
        if (root / path).is_file()
    ]
    documented.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / "docs" / "agents").glob("*.md"))
        if path.is_file()
    )
    return tuple(documented)


def read_spec(repo_root, reference):
    if reference is None:
        return "not provided", None
    candidate = pathlib.Path(reference)
    if not candidate.is_absolute():
        candidate = pathlib.Path(repo_root) / candidate
    if candidate.is_file():
        try:
            return reference, candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(
                f"spec file could not be read: {reference}: {error}"
            ) from error
    if "/" in reference or candidate.suffix:
        raise RuntimeError(f"spec file not found: {reference}")
    result = subprocess.run(
        ["gh", "issue", "view", reference, "--comments"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "gh issue view failed"
        raise RuntimeError(f"spec issue could not be read: {reference}: {detail}")
    return reference, result.stdout


def prepare_review(args):
    """Everything every requested axis needs, or the failure that there is not.

    Each requested axis's brief is built here rather than where its Lane opens,
    so an axis that cannot be briefed fails preparation — before this review is
    reported started, and before any pane exists to tear down.
    """
    repo_root = canonical_worktree_root(args.cwd)
    source, contents = read_spec(repo_root, args.spec)
    navigation_block = read_code_graph_navigation(repo_root, args.base)
    preparation = ReviewPreparation(
        fixed_point=args.base,
        resolved_fixed_point=args.resolved_base,
        commit_list=read_commit_list(args.cwd, args.base),
        spec_source=source,
        spec_contents=contents,
        standards_files=find_standards_files(repo_root),
        navigation_block=navigation_block,
        code_graph_used=navigation_block is not None,
    )
    for axis in requested_axes(args):
        preparation.brief(axis)
    return preparation


def preparation_report(args):
    return args.preparation.report() if args.preparation is not None else None


def build_prompt(brief, bridge_id):
    """One brief as the turn that carries it, marked so its own turn can be found again."""
    return f"[claude-tui-review-bridge:{bridge_id}]\n{brief.text}"


PROBE_BRIEF = (
    "This is a bridge health probe. Do not run commands, read files, or call "
    "tools. Reply with exactly: TUI_REVIEW_BRIDGE_OK"
)
BROWSER_PROBE_BRIEF = (
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


def resolve_axis_choice(args, axis, choice):
    """Resolve one axis's model or effort, with its own flag overriding the generic one."""
    axis_choice = getattr(args, f"{axis}_{choice}", None)
    if axis_choice is not None:
        return axis_choice
    return getattr(args, choice, None)


def validate_resume_axis(args, state):
    """Reject a caller axis that disagrees with the axis owned by the resume handle."""
    saved_axis = state["axis"]
    if args.axis != saved_axis:
        raise RuntimeError(
            f"Review session {args.resume_session} resumes axis "
            f"'{saved_axis}', but --axis is '{args.axis}'"
        )


def resume_state_for_review(args):
    """Read and validate a resumed handle before this review is reported started."""
    if not args.resume_session:
        return None
    state = SessionStore().read(args.resume_session)
    validate_resume_axis(args, state)
    return state


def requested_axes(args):
    """The axes this call asked for, in the order a review runs them."""
    return ("standards", "spec") if args.axis == "both" else (args.axis,)


def axis_brief(args, axis):
    """One axis's Brief: what preparation filled, or a probe's fixed text.

    A health probe is prepared for nothing and carries no axis of its own, so it
    brings its own text and delivers whatever `--axis` says as a single Lane.
    """
    if args.browser_probe:
        return AxisBrief(axis=axis, text=BROWSER_PROBE_BRIEF)
    if args.probe:
        return AxisBrief(axis=axis, text=PROBE_BRIEF)
    return args.preparation.brief(axis)


def axis_briefs(args):
    """Every Brief this call delivers, in the order a review runs them."""
    if args.probe or args.browser_probe:
        return (axis_brief(args, args.axis),)
    return args.preparation.briefs(requested_axes(args))


def review_start_model(args, resume_state=None):
    """The model every requested axis shares, including a resumed axis's saved pin.

    Axes pinned to different models share none, and a review that starts on more
    than one model can name no single one truthfully.
    """
    if resume_state is not None:
        selected = resolve_axis_choice(args, resume_state["axis"], "model")
        return selected if selected is not None else resume_state.get("model")
    models = [resolve_axis_choice(args, axis, "model") for axis in requested_axes(args)]
    return models[0] if all(model == models[0] for model in models[1:]) else None


def run_pane(args):
    runtime_dir = pathlib.Path(args.runtime_dir)
    socket_path = runtime_dir / "app-server.sock"
    log_path = runtime_dir / "app-server.log"
    log_file = log_path.open("a", encoding="utf-8")
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


def cleanup_pane(pane_id, runtime_dir):
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
        (
            "-v"
            if getattr(args, "split_direction", "horizontal") == "vertical"
            else "-h"
        ),
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
    return result.stdout.strip()


def open_child_pane(args, runtime_dir):
    """Open this axis's pane, and tell the caller's hook which child it got."""
    pane_id = launch_pane(args, runtime_dir)
    hook_child_launch(args, tmux_target=pane_id)
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
    marker, axis,
):
    now = time.time()
    return {
        "version": SESSION_STATE_VERSION,
        "reviewSessionId": session_id,
        "axis": axis,
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

    The resumed record decides the axis. For each choice, that axis's flag
    overrides the generic flag; with neither, the follow-up inherits whatever
    its first review pinned. A lineage that was never pinned stays unpinned.
    """
    axis = state["axis"]
    for choice in ("model", "effort"):
        selected = resolve_axis_choice(args, axis, choice)
        if selected is not None:
            state[choice] = selected
        setattr(args, choice, state.get(choice))


def update_session_after_turn(state, turn, target):
    state["target"] = target
    state["lastTurnId"] = turn.get("id")
    state["lastStatus"] = turn.get("status")
    state["updatedAt"] = time.time()


def axis_result(state, turn, final_message):
    status = turn.get("status")
    if status == "completed" and not final_message:
        status = "failed"
    result = {
        "status": status,
        "finalMessage": final_message,
        "reviewSessionId": state["reviewSessionId"],
    }
    if status != "completed":
        if turn.get("status") == "completed":
            result["reason"] = "review completed without a final message"
        else:
            result["reason"] = (
                f"review turn ended with status {turn.get('status') or 'unknown'}"
            )
    return result


@dataclasses.dataclass(frozen=True)
class AxisLaunch:
    args: argparse.Namespace
    prompt: str
    marker: str
    runtime_dir: pathlib.Path
    pane_id: str


@dataclasses.dataclass(frozen=True)
class AxisCompleted:
    state: dict
    turn: dict

    @property
    def thread_id(self):
        return self.state["threadId"]


@dataclasses.dataclass(frozen=True)
class AxisFailure:
    state: dict | None
    thread_id: str | None
    reason: str


class AxisRunError(Exception):
    def __init__(self, state, thread_id, reason):
        super().__init__(reason)
        self.state = state
        self.thread_id = thread_id
        self.reason = reason


def launch_axis(args, brief, tmux_target, split_direction):
    axis_args = argparse.Namespace(**vars(args))
    axis_args.axis = brief.axis
    for choice in ("model", "effort"):
        setattr(axis_args, choice, resolve_axis_choice(args, brief.axis, choice))
    axis_args.tmux_target = tmux_target
    axis_args.split_direction = split_direction
    bridge_id = str(uuid.uuid4())
    marker = f"[claude-tui-review-bridge:{bridge_id}]"
    prompt = build_prompt(brief, bridge_id)
    runtime_dir = make_runtime(prompt)
    try:
        pane_id = open_child_pane(axis_args, runtime_dir)
    except Exception:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        raise
    return AxisLaunch(axis_args, prompt, marker, runtime_dir, pane_id)


async def drive_new_review(launch, owner, store):
    args = launch.args
    runtime_dir = launch.runtime_dir
    pane_id = launch.pane_id
    client = None
    state = None
    thread_id = None
    try:
        client = await connect_when_ready(
            runtime_dir / "app-server.sock",
            pane_id,
            args.startup_timeout,
            runtime_dir / "app-server.log",
        )
        thread_id = await start_thread(
            client, cwd=args.cwd, sandbox=args.sandbox, approval=args.approval
        )
        await wait_for_mcp_startup(client, pane_id, args.startup_timeout)
        await start_turn(
            client, thread_id, launch.prompt, args.model, args.effort
        )
        session_id = str(uuid.uuid4())
        state = session_state(
            session_id,
            owner,
            runtime_dir,
            pane_id,
            thread_id,
            args.base,
            args.model,
            args.effort,
            launch.marker,
            args.axis,
        )
        state["preparation"] = preparation_report(args)
        # The record goes down before the pane is told to attach. Handing
        # over first would let a driver killed in between leave a live pane
        # running a review that `--recover-session` can no longer find.
        store.write(session_id, state)
        # Only now does the thread have a rollout for the TUI to resume.
        hand_off_thread(runtime_dir, thread_id)
        _thread, turn = await wait_for_review(
            client, thread_id, launch.marker, pane_id, args.timeout
        )
    except Exception as error:
        reason = str(error) or type(error).__name__
        raise AxisRunError(state, thread_id, reason) from error
    finally:
        if client is not None:
            await client.__aexit__(None, None, None)
    return AxisCompleted(state, turn)


async def drive_terminal_axis(launch, owner, store):
    try:
        result = await drive_new_review(launch, owner, store)
    except AxisRunError as error:
        cleanup_pane(launch.pane_id, launch.runtime_dir)
        return AxisFailure(error.state, error.thread_id, error.reason)
    cleanup_pane(launch.pane_id, launch.runtime_dir)
    return result


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
        pane_id = open_child_pane(args, runtime_dir)
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
        cleanup_pane(pane_id, runtime_dir)
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


async def run_existing_review(args, brief, owner, store):
    state = args.resume_state
    if state is None:
        state = store.read(args.resume_session)
    validate_resume_axis(args, state)
    validate_session_owner(state, owner)
    apply_session_model_choice(args, state)
    bridge_id = str(uuid.uuid4())
    marker = f"[claude-tui-review-bridge:{bridge_id}]"
    prompt = build_prompt(brief, bridge_id)
    # On disk before the turn is awaited, for the same reason the first review
    # writes its record early: a driver killed mid-turn must leave a marker the
    # recovery path can wait on.
    state["marker"] = marker

    try:
        client = await connect_existing_session(state)
        if client is None:
            _thread, turn = await resume_session_in_new_pane(
                args, owner, store, state, prompt, marker
            )
        else:
            try:
                store.write(state["reviewSessionId"], state)
                await start_followup_turn(client, state, prompt)
                _thread, turn = await wait_for_review(
                    client,
                    state["threadId"],
                    marker,
                    state["paneId"],
                    args.timeout,
                )
            finally:
                await client.__aexit__(None, None, None)
    except Exception as error:
        return AxisFailure(
            state,
            state["threadId"],
            str(error) or type(error).__name__,
        )
    return AxisCompleted(state, turn)


async def run_recovered_reviews(args, owner, store):
    """Re-attach to every review axis this owner already has running.

    Returns each completed or failed axis for the sessions this owner already owns.

    The caller reaching here has lost the handle its driver was going to print —
    the driver was killed, or its output never arrived. Everything needed to pick
    the review back up survived that death: the record on disk, the pane, and the
    thread. So this starts no pane and no thread; it finds the owner's live
    sessions and waits on the turns already in flight. With no such session it
    raises `NoLiveSessionError` rather than falling back to a first review.
    """
    async def recover(state):
        client = await connect_existing_session(state)
        if client is None:
            return None
        try:
            try:
                _thread, turn = await wait_for_review(
                    client,
                    state["threadId"],
                    state["marker"],
                    state["paneId"],
                    args.timeout,
                )
            except Exception as error:
                return AxisFailure(
                    state,
                    state["threadId"],
                    str(error) or type(error).__name__,
                )
        finally:
            await client.__aexit__(None, None, None)
        return AxisCompleted(state, turn)

    recovered = await asyncio.gather(
        *(recover(state) for state in store.find_by_owner(owner))
    )
    live = [run for run in recovered if run is not None]
    if live:
        return {run.state["axis"]: run for run in live}
    raise NoLiveSessionError(
        "No live review session for this tmux pane and worktree. "
        "Nothing to recover; start a review instead."
    )


class CodexLane:
    """Delivery to the codex reviewer: one Codex TUI pane per axis.

    The seam every Lane is reached through. Preparation is finished by the time
    one is opened, so a Lane takes one Axis Brief and gives back that axis's
    result; everything that is codex's rather than the review's — panes, threads,
    app-server connections, the record on disk — lives on this side of it.
    """

    name = REVIEWER_CODEX

    def __init__(self, args, owner, store):
        self.args = args
        self.owner = owner
        self.store = store
        # Pane layout: the first axis splits off the caller's own pane to its
        # right, and each further axis splits off the one before it, downwards.
        self.previous_pane = None

    def open(self, brief):
        """Open one axis's pane, ready to be driven."""
        launch = launch_axis(
            self.args,
            brief,
            self.previous_pane or self.owner.origin_pane,
            "vertical" if self.previous_pane else "horizontal",
        )
        self.previous_pane = launch.pane_id
        return launch

    def discard(self, launch):
        """Tear down an axis that opened but will never be driven."""
        cleanup_pane(launch.pane_id, launch.runtime_dir)

    async def deliver(self, launch):
        """Drive one opened axis to a result of its own."""
        return await drive_terminal_axis(launch, self.owner, self.store)

    async def resume(self, brief):
        """Put one more turn to the lineage a resume handle names."""
        return await run_existing_review(self.args, brief, self.owner, self.store)

    async def recover(self):
        """Re-attach to every axis this owner already has running."""
        return await run_recovered_reviews(self.args, self.owner, self.store)

    def settle(self, run):
        """Close one axis out: its record written, its pane down, its result returned."""
        args = self.args
        handed_back = args.recover_session or args.resume_session
        if isinstance(run, AxisFailure):
            if handed_back:
                cleanup_pane(
                    run.state.get("paneId") if run.state else None,
                    run.state.get("runtimeDir") if run.state else None,
                )
            return {
                "status": FAILED_STATUS,
                "finalMessage": "",
                "reviewSessionId": (
                    run.state["reviewSessionId"] if run.state else None
                ),
                "reason": run.reason,
            }
        state, turn = run.state, run.turn
        final_message = final_agent_message(turn)
        target = state["target"] if args.recover_session else args.base
        update_session_after_turn(state, turn, target)
        if not args.recover_session:
            state["preparation"] = preparation_report(args)
        self.store.write(state["reviewSessionId"], state)
        if handed_back:
            cleanup_pane(state.get("paneId"), state.get("runtimeDir"))
        return axis_result(state, turn, final_message)

    def end_axis(self, axis, result, run):
        """End an axis this Lane drove to a result of its own.

        What an axis spent is read from the Codex rollout its thread wrote, which
        is why the point is reported from here rather than from the harness.
        """
        hook_axis_end(
            self.args,
            axis,
            result["status"],
            result["reviewSessionId"],
            run.thread_id,
        )

    def recovered_preparation(self, runs):
        """What a recovered review was prepared from, read back off its own records."""
        return next(
            (
                run.state.get("preparation")
                for run in runs
                if run.state is not None
            ),
            None,
        )


# Every reviewing vendor there is a Lane for, keyed by the name `--reviewer`
# takes. The keys are the argument's accepted values, so a Lane cannot be
# reachable by a name the command line rejects, or rejected by one it accepts.
LANES = {CodexLane.name: CodexLane}


def resolve_lane(args, owner, store):
    """The Lane this call named, before any of it opens."""
    lane = LANES.get(args.reviewer)
    if lane is None:
        known = ", ".join(sorted(LANES))
        raise RuntimeError(
            f"Unknown reviewer for --reviewer: {args.reviewer!r}; "
            f"known reviewers: {known}"
        )
    return lane(args, owner, store)


async def deliver_briefs(args, lane, briefs):
    """Every requested axis's result, delivered concurrently through one Lane.

    Every axis is opened before any is driven, so a Lane that cannot open them
    all opens none: a review that would be half delivered is no review at all,
    and the axes that did open are torn down before their turns begin.
    """
    launches = []
    try:
        for brief in briefs:
            launches.append(lane.open(brief))
    except Exception:
        for brief, launch in zip(briefs, launches, strict=False):
            lane.discard(launch)
            end_launched_axis(args, brief.axis)
        raise
    runs = await asyncio.gather(*(lane.deliver(launch) for launch in launches))
    return {brief.axis: run for brief, run in zip(briefs, runs, strict=True)}


async def run_bridge(args):
    if not pathlib.Path(args.cwd).is_dir():
        raise RuntimeError(f"Working directory does not exist: {args.cwd}")
    probe = args.probe or args.browser_probe
    if not args.recover_session and not probe:
        args.resolved_base = resolve_fixed_point(args.cwd, args.base)
        ensure_nonempty_diff(args.cwd, args.base)
        args.preparation = prepare_review(args)
    elif probe:
        args.preparation = None
    # Preparation has succeeded and no Lane has opened yet, which is what this
    # point promises: a review that failed before here never started.
    hook_review_start(args, review_start_model(args, args.resume_state))
    owner = resolve_owner(args)
    store = SessionStore()
    lane = resolve_lane(args, owner, store)
    # The lock stays process-scoped: it serialises concurrent calls from one
    # pane, and a driver that dies releases it. Duplicate prevention across a
    # driver's death is the recovery path's job, not a longer-lived lock's — a
    # lock that outlived its holder would also have to be reaped, and would
    # block the very recovery call that clears the duplicate.
    with owner_lock(store, owner):
        if args.recover_session:
            runs = await lane.recover()
        elif args.resume_session:
            runs = {args.axis: await lane.resume(axis_brief(args, args.axis))}
        else:
            runs = await deliver_briefs(args, lane, axis_briefs(args))

        results = {}
        for axis, run in runs.items():
            result = lane.settle(run)
            if args.recover_session:
                result["recovered"] = True
            results[axis] = result
    # Once every turn has settled, so each rollout's last cumulative count is the
    # whole of what its axis spent.
    for axis, run in runs.items():
        lane.end_axis(axis, results[axis], run)
    if args.recover_session:
        preparation = lane.recovered_preparation(runs.values())
    else:
        preparation = preparation_report(args)
    output = {
        "status": (
            "completed"
            if all(
                result["status"] == "completed" and result["finalMessage"]
                for result in results.values()
            )
            else "partially_completed"
        ),
        "axes": results,
        "preparation": preparation,
    }
    print(json.dumps(output, ensure_ascii=False))
    args.status = output["status"]
    succeeded = output["status"] == "completed"
    return 0 if succeeded else 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Launch or resume an interactive Codex TUI review."
    )
    parser.add_argument(
        "--reviewer",
        required=True,
        choices=tuple(LANES),
        help="reviewing vendor this review is delivered to",
    )
    parser.add_argument("--base", help="fixed point for the three-dot diff")
    parser.add_argument("--spec", help="issue reference or file path for the spec")
    parser.add_argument(
        "--axis",
        choices=("standards", "spec", "both"),
        default="both",
        help="review axis to run (default: both)",
    )
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
    parser.add_argument(
        "--standards-model",
        help="Codex model for the standards axis (default: --model)",
    )
    parser.add_argument(
        "--standards-effort",
        help="Reasoning effort for the standards axis (default: --effort)",
    )
    parser.add_argument(
        "--spec-model",
        help="Codex model for the spec axis (default: --model)",
    )
    parser.add_argument(
        "--spec-effort",
        help="Reasoning effort for the spec axis (default: --effort)",
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
    for point, fires in HOOK_POINTS.items():
        parser.add_argument(
            f"--on-{point}",
            help=f"command to run {fires}, in the reviewed working directory "
                 f"with this review's facts in its environment (default: "
                 f"nothing runs)",
        )
    parser.add_argument("--tmux-target", help=argparse.SUPPRESS)
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--browser-probe", action="store_true", help=argparse.SUPPRESS)
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.recover_session:
        if args.resume_session:
            parser.error("--recover-session and --resume-session are exclusive")
    elif not (args.probe or args.browser_probe) and not args.base:
        parser.error("--base is required")
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
    # A review no result was ever reached for is a failed one, until the result
    # itself says otherwise.
    args.status = FAILED_STATUS
    args.resume_state = None
    # The end point straddles the whole call, so it fires on every exit path this
    # bridge controls — a review that failed, timed out, or raised included,
    # including one that failed before any Lane opened.
    try:
        try:
            args.resume_state = resume_state_for_review(args)
        except (OSError, RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1
        try:
            return asyncio.run(run_bridge(args))
        except NoLiveSessionError as error:
            print(str(error), file=sys.stderr)
            return NO_LIVE_SESSION_EXIT
        except (AppServerError, OSError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 1
    finally:
        hook_review_end(args, args.status)


if __name__ == "__main__":
    raise SystemExit(main())
