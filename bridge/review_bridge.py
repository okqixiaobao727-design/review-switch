#!/usr/bin/env python3

"""Code-review channel between a caller and the reviewing vendor it named.

Round one starts a review lineage; a round the Rounds Contract still allows
resumes it, and `--recover-session` waits on a live reviewer or hands back the
undelivered report a killed driver left behind. That contract is held here
rather than stated anywhere: every result names the one action its caller is
permitted next, and a resume past the cap is refused. A fresh lineage is always
available.

Preparation and delivery are separate: preparation fills one Axis Brief per
requested axis, and the Lane `--reviewer` names takes one brief and gives back
that axis's result. A reviewer this bridge has no Lane for is refused by name
before any of it opens. `codex` drives an interactive TUI lineage in a tmux pane
of its own; `claude` drives a headless process and needs no tmux (ADR-0003).

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
    from aiohttp import web
except ImportError as error:
    raise SystemExit(
        "review_bridge requires Python package 'aiohttp'. "
        "Install it for the Python interpreter used by Claude Code."
    ) from error

TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
MCP_STARTUP_NOTIFICATION = "mcpServer/startupStatus/updated"
MCP_STARTUP_TERMINAL_STATES = {"ready", "failed", "cancelled"}
DEFAULT_TIMEOUT_SECONDS = 7200
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60
MCP_STARTUP_LOG_FILENAME = "mcp-startup.jsonl"
TUI_PROXY_SOCKET_FILENAME = "tui-proxy.sock"
TUI_PROXY_LOG_FILENAME = "tui-proxy.log"
SESSION_STATE_VERSION = 2
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Recovery found no live reviewer or undelivered report, which is different from a
# failed review: it is the one result that licenses starting a first review.
NO_LIVE_SESSION_EXIT = 3
# The reviewing vendor a caller names on `--reviewer`. The model is whatever the
# lineage was pinned to, so the reviewer names the vendor and nothing else.
REVIEWER_CODEX = "codex"
REVIEWER_CLAUDE = "claude"
CODE_GRAPH_CLI = "code-review-graph"
# The tool's own "put the graph somewhere else" variable, which the Bridge
# takes out of the environment it hands the CLI. One data directory holds one
# graph — measured: two repositories built against one `CRG_DATA_DIR` leave a
# single `graph.db`, the second build evicting the first — so honouring it
# would give every checkout under review the previous one's map. A checkout
# owns its graph (ADR-0005), which is a rule about where the graph is, not a
# preference an operator's environment can settle.
CODE_GRAPH_DATA_DIR_VAR = "CRG_DATA_DIR"
# A build is the one call in the graph flow whose cost is the whole checkout
# rather than the change, and it scales with source-file count: 1.75 s for 53
# files, measured in ADR-0005. The bound is that with room for a repository
# some fifty times the size, and is deliberately not configurable — a review
# that outgrows it falls back to running without the graph, as any other
# failure of the tool does.
CODE_GRAPH_BUILD_TIMEOUT_SECONDS = 120
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
    "REVIEW_REPORT_FILE",
    *COUNTER_VARS.values(),
})
# What a review that never reached a result of its own is: the result contract's
# spelling for a failure, which is what both status-carrying points use.
FAILED_STATUS = "failed"
# A resume this contract has no round left for. Held apart from a failure: no
# Lane opened, so nothing was reviewed and nothing was billed.
REFUSED_STATUS = "refused"
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
    """No recoverable review session belongs to the caller's owner identity."""


def has_report(final_message):
    """Whether an axis came back with a report at all.

    Whitespace is no more a report than nothing is, and one rule decides it for
    everything that asks — whether the axis completed, and whether there is a
    file to write. Two thresholds would let an axis complete and still have no
    report to point its caller at.
    """
    return bool(final_message and final_message.strip())


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


def hook_axis_end(args, axis, status, session, report_file, cost):
    """End one axis: how it finished, where its report is, and what it spent.

    What an axis spent is the Lane's to read — a rollout on one Lane, a printed
    result on the other — so it arrives here already read, as the counters or as
    the reason there are none. The model reported is the one the reviewer
    resolved to and nothing else: the alias the caller asked for is already
    theirs to remember. The report is named rather than carried: a hook command
    that wants the text opens the file, and one that does not pays nothing for
    it.
    """
    counters, model, detail = cost
    run_hook(
        args,
        AXIS_END,
        REVIEW_AXIS=axis,
        REVIEW_STATUS=status,
        REVIEW_SESSION=session or "",
        REVIEW_REPORT_FILE=report_file or "",
        REVIEW_MODEL=model or "",
        **cost_facts(counters, detail),
    )


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


def printable_git_command(arguments):
    return " ".join(("git", *arguments))


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


@dataclasses.dataclass(frozen=True)
class ReviewScope:
    """The fixed point to the working tree as it stands, committed or not.

    The tree is read live rather than snapshotted, so a Scope holds only as
    long as the tree does; the Axis Brief asks for it to be left alone
    (ADR-0004).
    """

    fixed_point: str
    resolved_fixed_point: str
    fork_point: str

    UNTRACKED_ARGUMENTS = ("ls-files", "--others", "--exclude-standard")

    @property
    def diff_arguments(self):
        """A diff of the tree against the fork point: committed, staged, and unstaged."""
        return ("diff", self.fork_point)

    @property
    def emptiness_arguments(self):
        """The same diff, asked only whether it is empty; --quiet must precede the rev."""
        return ("diff", "--quiet", self.fork_point, "--")

    @property
    def diff_command(self):
        return printable_git_command(self.diff_arguments)

    @property
    def untracked_command(self):
        """The new files no diff can show, which is why the brief prints two lines."""
        return printable_git_command(self.UNTRACKED_ARGUMENTS)


def resolve_review_scope(cwd, fixed_point):
    """The Scope a review runs over, or the failure that the fixed point is not one."""
    resolved = run_git(cwd, "rev-parse", "--verify", f"{fixed_point}^{{commit}}")
    fork = run_git(cwd, "merge-base", fixed_point, "HEAD")
    if (
        resolved.returncode != 0
        or not resolved.stdout.strip()
        or fork.returncode != 0
        or not fork.stdout.strip()
    ):
        raise RuntimeError(f"fixed point did not resolve: {fixed_point}")
    return ReviewScope(
        fixed_point=fixed_point,
        resolved_fixed_point=resolved.stdout.strip(),
        fork_point=fork.stdout.strip(),
    )


def ensure_scope_holds_work(cwd, scope):
    """Raises when the tree matches the fixed point, which is nothing to review."""
    diff = run_git(cwd, *scope.emptiness_arguments)
    if diff.returncode == 1:
        return
    if diff.returncode != 0:
        detail = diff.stderr.strip() or "git diff failed"
        raise RuntimeError(
            f"review scope could not be read for {scope.fixed_point}: {detail}"
        )
    untracked = run_git(cwd, *scope.UNTRACKED_ARGUMENTS)
    if untracked.returncode != 0:
        detail = untracked.stderr.strip() or "git ls-files failed"
        raise RuntimeError(
            f"review scope could not be read for {scope.fixed_point}: {detail}"
        )
    if not untracked.stdout.strip():
        raise RuntimeError(
            "nothing to review: the working tree matches the fixed point "
            f"{scope.fixed_point}"
        )


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


def lane_owner(args, lane):
    """The identity this call's sessions belong to, tmux half filled or not.

    The tuple is the Bridge's and every Lane is keyed by it. A Lane with no
    window has no tmux half to fill, which is also what keeps a headless Lane's
    records and a codex Lane's from ever matching each other's owner.
    """
    if lane.NEEDS_TMUX:
        return resolve_owner(args)
    return InvocationOwner(
        tmux_server="",
        origin_pane="",
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

    def report_path(self, session_id):
        return self.root / f"{self._safe_id(session_id)}.md"

    def spec_path(self, spec_id):
        return self.root / f"{self._safe_id(spec_id)}-spec.md"

    def _write_readable(self, destination, contents):
        """Put one file in the store where a human or a Lane can open it, and name it.

        Every file the store hands a path to is written the same way — private
        to its owner, and named by the store rather than by whoever asked — so
        the mode is set in one place instead of once per kind of file.
        """
        destination.write_text(contents, encoding="utf-8")
        os.chmod(destination, 0o600)
        return str(destination)

    def write_spec(self, contents):
        """Write the fetched spec where a Lane can open it, and name the file.

        Its own id rather than the review's: the spec is written while the
        brief that names it is filled, which is before any session exists to
        name it after, and one per preparation is what keeps two reviews
        running at once from reading each other's spec.
        """
        return self._write_readable(
            self.spec_path(str(uuid.uuid4())), contents
        )

    def write_report(self, session_id, final_message):
        """Write one axis's report where a human can open it, and name the file.

        A report with nothing in it is no report: an empty file would be a path
        that leads nowhere, so there is none and the caller is told so.
        """
        if not has_report(final_message):
            return None
        return self._write_readable(
            self.report_path(session_id), final_message
        )

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

    def remove(self, session_id):
        """Take back a record whose review never began; missing is already done."""
        self.state_path(session_id).unlink(missing_ok=True)

    def find_by_owner(self, owner, required=()):
        """Returns every record this owner wrote carrying `required`, newest first.

        The record is written before the review is awaited, so a session whose
        driver died mid-review is already on disk under the same owner tuple the
        resume path validates. What makes a record reachable again is the Lane's
        to say: either the fields needed to find its reviewer, or an undelivered
        stored report. Anything unreadable or written by another version is
        skipped rather than raised — one damaged file must not hide a healthy
        session.
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
            if any(not state.get(field) for field in required):
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
                "Another review bridge call is already running for "
                + ("this tmux pane" if owner.origin_pane else "this worktree")
            ) from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class AppServerClient:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.next_id = 1

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
                    # The durable user-message queue is currently exposed by
                    # app-server under its experimental protocol capability.
                    "capabilities": {"experimentalApi": True},
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

    async def _handle_unsolicited(self, payload):
        """Decline a server request; ordinary notifications need no response."""
        if payload.get("id") is not None and payload.get("method"):
            await self.websocket.send_json(
                {
                    "id": payload["id"],
                    "error": {
                        "code": -32601,
                        "message": (
                            "The read-only review Bridge does not answer "
                            f"server request {payload['method']}."
                        ),
                    },
                }
            )


def record_mcp_startup_notification(path, payload):
    """Append one thread-scoped MCP transition observed on the TUI connection."""
    if payload.get("method") != MCP_STARTUP_NOTIFICATION:
        return
    params = payload.get("params") or {}
    if not params.get("threadId") or not params.get("name"):
        return
    with pathlib.Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(params, ensure_ascii=False) + "\n")
        stream.flush()


def read_mcp_startup_statuses(path, thread_id):
    """Return each server's latest complete proxy record for one thread."""
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    statuses = {}
    for line in lines:
        try:
            status = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            status.get("threadId") == thread_id
            and status.get("name")
            and status.get("status")
        ):
            statuses[status["name"]] = status
    return statuses


STANDARDS_BRIEF_TEMPLATE = """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: {diff_command}
New files not in that diff: {untracked_command}
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


def read_code_graph_navigation(checkout, fork_point):
    """The navigation block for the Scope, or nothing when the graph can't see it.

    A checkout owns its graph, so the CLI is pointed at the one under review —
    a linked worktree as readily as a main checkout, since a worktree's graph
    lives in the worktree and dies with it (ADR-0005). Where nothing has been
    built the Bridge builds it, and where something has it updates instead:
    the two are exclusive, because a build re-parses every file the update
    would have. Three calls, then, however the checkout arrives — and none of
    them inherits the operator's `CRG_DATA_DIR`, which would put every
    checkout's graph in the one file.

    The base is the Scope's fork point, not the fixed point it was named by, so
    the graph's changed set covers exactly the range the Axis Brief's diff does.
    Uncommitted work is in that range: build and `update --brief` alike re-parse
    from disk before `detect-changes` reads the graph, and a git diff base
    compares against the working tree. So a dirty tree is no reason to skip.
    """
    executable = shutil.which(CODE_GRAPH_CLI)
    if executable is None:
        return None

    environment = {
        name: value
        for name, value in os.environ.items()
        if name != CODE_GRAPH_DATA_DIR_VAR
    }

    def graph_call(arguments, timeout=None):
        return subprocess.run(
            [executable, *arguments, "--repo", checkout],
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    scoped_arguments = ["--base", fork_point]
    try:
        status = graph_call(["status", "--json"])
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
        if graph_exists:
            refresh = graph_call(["update", "--brief", *scoped_arguments])
        else:
            refresh = graph_call(
                ["build"], timeout=CODE_GRAPH_BUILD_TIMEOUT_SECONDS
            )
        if refresh.returncode != 0:
            return None
        detection = graph_call(["detect-changes", *scoped_arguments])
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    if detection.returncode != 0:
        return None
    try:
        result = json.loads(detection.stdout)
        return build_navigation_block(result, checkout)
    except (KeyError, TypeError, ValueError):
        return None


def build_standards_brief(scope, commit_list, standards_files):
    return STANDARDS_BRIEF_TEMPLATE.format(
        diff_command=scope.diff_command,
        untracked_command=scope.untracked_command,
        commit_list=with_trailing_newline(commit_list),
        standards_files=(
            ", ".join(standards_files)
            if standards_files
            else "none documented; baseline only"
        ),
    )


@dataclasses.dataclass(frozen=True)
class SpecSlot:
    """The Spec slot of an Axis Brief, and the file it sends the Lane to.

    Every way of naming a spec ends here — an issue the Bridge wrote out, a
    file already in the checkout, a reference it could not fetch, or none at
    all — so the brief is filled from one shape rather than from four. `text`
    is the slot as the Lane reads it and is `None` only when no reference was
    given, which is the one case the Spec axis cannot run on. `file` is the
    spec the review was held to, and is the same path the receipt reports.
    """

    source: str
    text: str | None
    file: str | None = None


SPEC_SLOT_TEMPLATE = "Spec: {path}{summary}. Read it before reviewing."


def build_spec_slot(path, summary=None):
    """The one line that sends a Lane to the spec, saying what it will find.

    Naming the file rather than pasting it keeps a long comment thread out of
    every first turn, and lets a Lane read the body before the thread; the
    brief's closing instruction to quote the spec line is what keeps reading it
    from being optional (#33). A spec already in the checkout carries no
    summary: the Lane can see for itself what a path it was given holds.
    """
    return SPEC_SLOT_TEMPLATE.format(
        path=path, summary=f" — {summary}" if summary else ""
    )


SPEC_BRIEF_TEMPLATE = """Read-only review: report findings; leave the working tree untouched. Review it yourself in this session.

Diff: {diff_command}
New files not in that diff: {untracked_command}
Commits:
{commit_list}
{spec_slot}
Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words."""


def build_spec_brief(scope, commit_list, spec_slot):
    return SPEC_BRIEF_TEMPLATE.format(
        diff_command=scope.diff_command,
        untracked_command=scope.untracked_command,
        commit_list=with_trailing_newline(commit_list),
        spec_slot=with_trailing_newline(spec_slot),
    )


@dataclasses.dataclass(frozen=True)
class ReviewPreparation:
    scope: ReviewScope
    commit_list: str
    spec: SpecSlot
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
                    self.scope, self.commit_list, self.standards_files
                ),
                self.navigation_block,
            )
        if axis == "spec":
            if self.spec.text is None:
                raise RuntimeError(
                    "spec source was not provided; run with --axis standards "
                    "when no spec exists"
                )
            return append_navigation_block(
                build_spec_brief(
                    self.scope, self.commit_list, self.spec.text
                ),
                self.navigation_block,
            )
        raise RuntimeError("axis 'both' requires the two-pane fan-out")

    def report(self):
        return {
            "fixedPoint": self.scope.resolved_fixed_point,
            "specSource": self.spec.source,
            "specFile": self.spec.file,
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


UNFETCHED_SPEC_TEMPLATE = """Spec:
The spec could not be fetched, so this review has no requirements to check against.
Reference as given: {reference}
Failure: {detail}
Report exactly that: the spec was unreachable, naming the reference and the failure above. Do not infer from the diff, the commits, or the code what the spec asked for, and report no other spec finding."""


def spec_not_fetched(reference, detail):
    """Every way a spec goes missing, as the one thing that happens next.

    A reference that cannot be fetched costs the caller a weaker review, never
    the review: the Spec slot carries the reference and the failure, and the
    source records that the spec was not fetched, so a spec-less review is told
    apart from an ordinary one without re-deriving it (#30). There is no file
    to name, so the receipt names none (#33).
    """
    return SpecSlot(
        source=f"not fetched: {reference}",
        text=UNFETCHED_SPEC_TEMPLATE.format(
            reference=reference, detail=detail
        ),
    )


#: What the Bridge asks `gh` for. Naming the fields is what makes "this issue
#: states no requirements" distinguishable from "the requirements were never
#: requested" — `--comments` replaces the body with the thread rather than
#: adding to it, and its return code says nothing about either (#30).
ISSUE_SPEC_FIELDS = "number,title,body,comments"


@dataclasses.dataclass(frozen=True)
class IssueSpec:
    """One fetched issue: the text written out, and what the slot says is in it."""

    contents: str
    summary: str


def describe_issue_parts(has_body, comment_count):
    """What a written-out issue holds, for the Lane deciding how much to read."""
    parts = ["body"] if has_body else []
    if comment_count:
        parts.append(
            "1 comment" if comment_count == 1 else f"{comment_count} comments"
        )
    return " and ".join(parts)


def build_issue_spec(issue):
    """An issue as the Spec slot reads it, or nothing when it states nothing.

    The layout is the Bridge's, assembled from the fields it asked for, so what
    a Lane reads is testable here rather than owned by `gh`.
    """
    body = (issue["body"] or "").strip()
    comments = [
        comment
        for comment in issue["comments"]
        if (comment["body"] or "").strip()
    ]
    if not body and not comments:
        return None
    heading = f"#{issue['number']} {issue['title']}".strip()
    sections = [heading]
    if body:
        sections.append(body)
    for comment in comments:
        author = (comment.get("author") or {}).get("login") or "unknown"
        sections.append(f"Comment from {author}:\n{comment['body'].strip()}")
    return IssueSpec(
        contents="\n\n".join(sections),
        summary=f"{heading}, {describe_issue_parts(bool(body), len(comments))}",
    )


def read_issue_spec(repo_root, reference, store):
    try:
        result = subprocess.run(
            ["gh", "issue", "view", reference, "--json", ISSUE_SPEC_FIELDS],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        return spec_not_fetched(reference, f"gh could not be run: {error}")
    if result.returncode != 0:
        detail = result.stderr.strip() or "gh issue view failed"
        return spec_not_fetched(reference, f"gh issue view failed: {detail}")
    try:
        issue = build_issue_spec(json.loads(result.stdout))
    except (AttributeError, KeyError, TypeError, ValueError):
        return spec_not_fetched(
            reference, "gh issue view returned output that could not be read"
        )
    if issue is None:
        return spec_not_fetched(reference, "the issue has no body and no comments")
    # Beside the report files and never in the reviewed checkout, whose own
    # untracked files are part of the Review Scope: a spec dropped there would
    # be reviewed as work (#33).
    try:
        path = store.write_spec(issue.contents)
    except OSError as error:
        return spec_not_fetched(
            reference, f"spec file could not be written: {error}"
        )
    return SpecSlot(
        source=reference,
        text=build_spec_slot(path, issue.summary),
        file=path,
    )


def read_spec_file(reference, candidate):
    """A spec already in the checkout is named where it lies; nothing is copied."""
    try:
        contents = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return spec_not_fetched(
            reference, f"spec file could not be read: {error}"
        )
    if not contents.strip():
        return spec_not_fetched(reference, "spec file is empty")
    return SpecSlot(
        source=reference, text=build_spec_slot(reference), file=reference
    )


def read_spec(repo_root, reference, store):
    """The Spec slot a Lane is handed, however the reference turned out.

    Only a reference the caller never gave leaves the slot empty; every other
    outcome is text. So `None` keeps exactly one meaning — the Spec axis was
    asked for without a spec — and preparation still fails on that alone.
    """
    if reference is None:
        return SpecSlot(source="not provided", text=None)
    if reference.startswith(("http://", "https://")):
        return read_issue_spec(repo_root, reference, store)
    candidate = pathlib.Path(reference)
    if not candidate.is_absolute():
        candidate = pathlib.Path(repo_root) / candidate
    if candidate.is_file():
        return read_spec_file(reference, candidate)
    if "/" in reference or candidate.suffix:
        return spec_not_fetched(reference, "spec file not found")
    return read_issue_spec(repo_root, reference, store)


def ensure_store_is_outside_the_checkout(repo_root, store):
    """Refuse a state root the review would end up reviewing.

    The Review Scope is the tree as it stands, so a store inside the checkout
    feeds a review its own spec file — written during preparation, before the
    Scope is read — and feeds round two round one's report. That is the Scope
    being wrong rather than a spec being unreachable, and #30's rule that a
    missing spec costs the review nothing does not reach it: a review of a
    polluted Scope is worse than no review, so none runs (#33).
    """
    root = pathlib.Path(store.root).resolve()
    checkout = pathlib.Path(repo_root).resolve()
    if root == checkout or checkout in root.parents:
        raise RuntimeError(
            f"state directory is inside the reviewed checkout: {store.root}"
        )


def prepare_review(args, store):
    """Everything every requested axis needs, or the failure that there is not.

    Each requested axis's brief is built here rather than where its Lane opens,
    so an axis that cannot be briefed fails preparation — before this review is
    reported started, and before any pane exists to tear down. The store comes
    in because a fetched issue is written out here, where the brief that names
    it is filled, rather than at the delivery that reads the brief.
    """
    repo_root = canonical_worktree_root(args.cwd)
    ensure_store_is_outside_the_checkout(repo_root, store)
    spec = read_spec(repo_root, args.spec, store)
    navigation_block = read_code_graph_navigation(
        repo_root, args.scope.fork_point
    )
    preparation = ReviewPreparation(
        scope=args.scope,
        commit_list=read_commit_list(args.cwd, args.base),
        spec=spec,
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


def process_exists(pid):
    """Return whether a process id still names a live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def cleanup_orphaned_runtime(parent_pid, runtime_dir):
    """Remove pane state only when no parent remains to collect its evidence."""
    if not process_exists(parent_pid):
        shutil.rmtree(runtime_dir, ignore_errors=True)


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


# ---------------------------------------------------------------------------
# The Rounds Contract.
#
# The cap on one review lineage, held here and enforced rather than stated, so
# that it holds without depending on a reviewing model reading and obeying it.
# Standards findings are fixed in one pass and earn no re-review; spec findings
# that required fixes earn exactly one, scoped to those fixes. Every result
# names the one action its caller is permitted next, which is the only channel
# that reaches a child with no skill of ours to read.
#
# What escalation *is* stays the caller's: nothing here names the act, only the
# moment.
# ---------------------------------------------------------------------------
#: The one axis whose findings earn a re-review.
SPEC_AXIS = "spec"
#: How many rounds the spec axis earns: the first, and the one re-review.
SPEC_AXIS_ROUNDS = 2
#: How many rounds every other axis earns, the standards axis above all.
SINGLE_ROUND = 1
NEXT_FIX_AND_STOP = "fix and stop"
NEXT_FIX_THEN_ONE_RE_REVIEW = "fix then one re-review"
NEXT_ESCALATE = "escalate"


def rounds_per_lineage(axis):
    """How many rounds one lineage of this axis earns."""
    return SPEC_AXIS_ROUNDS if axis == SPEC_AXIS else SINGLE_ROUND


def rounds_had(state):
    """How many rounds this lineage has had.

    A review that reached no record of its own had the round it was in, and a
    record written before the count existed is a lineage that had exactly one.
    """
    return state.get("rounds", SINGLE_ROUND) if state else SINGLE_ROUND


def next_action(axis, rounds):
    """The one action a caller is permitted next on this lineage."""
    if rounds < rounds_per_lineage(axis):
        return NEXT_FIX_THEN_ONE_RE_REVIEW
    if rounds > SINGLE_ROUND:
        # The re-review this lineage earned has been had, and a finding that
        # survived it has no further round to go to.
        return NEXT_ESCALATE
    return NEXT_FIX_AND_STOP


def refusal_for(state):
    """The refusal this lineage's next round gets, or `None` when it has one.

    The lineage's own record decides the axis, which is what makes the cap per
    lineage: a lineage that reached its cap exhausts itself and no other, and a
    fresh review is always available.
    """
    axis = state["axis"]
    rounds = rounds_had(state)
    allowed = rounds_per_lineage(axis)
    if rounds < allowed:
        return None
    return {
        "status": REFUSED_STATUS,
        "axes": {
            axis: {
                "status": REFUSED_STATUS,
                "finalMessage": "",
                "reviewSessionId": state["reviewSessionId"],
                "reason": (
                    f"a {axis} axis earns {allowed} round(s) per review "
                    f"lineage, and this one has had {rounds}"
                ),
                "next": NEXT_ESCALATE,
            }
        },
        "preparation": None,
    }


def refuse_resume_past_cap(args, owner, store):
    """The refusal a resume gets before anything is prepared, or `None`.

    Read before preparation and before any Lane opens, so a resume the contract
    plainly has no round for costs its caller nothing. This is the early answer
    and not the binding one: the copy read here may already have been overtaken
    by a sibling call, so the round itself is granted under the lock instead.

    Whose session it is is settled first. A handle belonging to another owner is
    not this caller's to be told anything about, its rounds included.
    """
    if args.recover_session or not args.resume_session:
        return None
    state = args.resume_state or store.read(args.resume_session)
    validate_session_owner(state, owner)
    return refusal_for(state)


def grant_round(args, owner, store):
    """Take this resume's round, or the refusal that its lineage has none left.

    Read from disk and written back here, under the owner's lock and before the
    reviewer is launched. Both halves belong here. Reading: the copy a caller
    arrived with predates any round a sibling call has since taken, and a cap
    decided on that copy is a cap two calls can pass at once. Writing: a round
    consumed but never recorded is one the contract cannot hold the next call
    to, so the round is spent when it is granted rather than when it succeeds —
    a resume that then fails has still had it, and a fresh lineage is the way
    back.

    Returns the record the Lane resumes, and the refusal that it may not.
    """
    state = store.read(args.resume_session)
    validate_session_owner(state, owner)
    refusal = refusal_for(state)
    if refusal is not None:
        return state, refusal
    state["rounds"] = rounds_had(state) + 1
    store.write(state["reviewSessionId"], state)
    return state, None


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


async def forward_websocket(source, destination, startup_log=None):
    """Forward one WebSocket direction, optionally observing server messages."""
    async for message in source:
        if message.type == aiohttp.WSMsgType.TEXT:
            if (
                startup_log is not None
                and MCP_STARTUP_NOTIFICATION in message.data
            ):
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    payload = {}
                record_mcp_startup_notification(startup_log, payload)
            await destination.send_str(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        }:
            break


def build_tui_proxy_app(args):
    """Build the local `/rpc` proxy used by a UDS Codex TUI."""
    async def proxy(request):
        # A UDS Codex TUI upgrades `/rpc`. Do not impose aiohttp's smaller
        # default message limit on the app-server protocol passing through.
        downstream = web.WebSocketResponse(max_msg_size=0)
        await downstream.prepare(request)
        try:
            connector = aiohttp.UnixConnector(path=args.upstream_socket)
            async with aiohttp.ClientSession(connector=connector) as session:
                upstream_url = f"http://localhost{request.rel_url}"
                async with session.ws_connect(
                    upstream_url, max_msg_size=0
                ) as upstream:
                    tasks = {
                        asyncio.create_task(forward_websocket(downstream, upstream)),
                        asyncio.create_task(
                            forward_websocket(upstream, downstream, args.startup_log)
                        ),
                    }
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    results = await asyncio.gather(*done, return_exceptions=True)
                    error = next(
                        (result for result in results if isinstance(result, Exception)),
                        None,
                    )
                    if error is not None:
                        raise error
        except Exception as error:
            print(f"TUI proxy connection failed: {error}", file=sys.stderr, flush=True)
            raise
        finally:
            await downstream.close()
        return downstream

    app = web.Application()
    app.router.add_get("/rpc", proxy)
    return app


async def run_tui_proxy(args):
    """Transparently proxy the TUI while recording its real MCP transitions."""
    app = build_tui_proxy_app(args)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, args.listen_socket)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def run_pane(args):
    runtime_dir = pathlib.Path(args.runtime_dir)
    socket_path = runtime_dir / "app-server.sock"
    proxy_socket_path = runtime_dir / TUI_PROXY_SOCKET_FILENAME
    startup_log_path = runtime_dir / MCP_STARTUP_LOG_FILENAME
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
    proxy_process = None
    proxy_log_file = None

    def cleanup(*_ignored):
        if proxy_process is not None:
            terminate_process(proxy_process)
        terminate_process(app_server)
        if proxy_log_file is not None:
            proxy_log_file.close()
        log_file.close()
        cleanup_orphaned_runtime(args.parent_pid, runtime_dir)

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

        proxy_log_path = runtime_dir / TUI_PROXY_LOG_FILENAME
        proxy_log_file = proxy_log_path.open("a", encoding="utf-8")
        proxy_process = subprocess.Popen(
            [
                sys.executable,
                str(pathlib.Path(__file__).resolve()),
                "_tui_proxy",
                "--upstream-socket",
                str(socket_path),
                "--listen-socket",
                str(proxy_socket_path),
                "--startup-log",
                str(startup_log_path),
            ],
            cwd=args.cwd,
            stdout=proxy_log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        if not wait_for_path(proxy_socket_path, args.startup_timeout):
            detail = read_log_tail(proxy_log_path) or "TUI proxy socket did not appear"
            print(
                f"Codex TUI proxy failed to start: {detail}",
                file=sys.stderr,
            )
            return 1

        # The visible TUI owns thread creation or resumption. The Bridge then
        # discovers that idle thread over app-server before it delivers work.
        command = build_tui_command(
            args, proxy_socket_path, getattr(args, "resume_thread_id", None)
        )
        return subprocess.run(command, cwd=args.cwd, check=False).returncode
    finally:
        cleanup()


def build_tui_command(args, socket_path, thread_id=None):
    """Return a visible TUI command that starts with no Axis Brief in flight."""
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
    if thread_id:
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
        "--parent-pid",
        str(os.getpid()),
    ]
    if args.network:
        pane_command.append("--network")
    if getattr(args, "model", None):
        pane_command.extend(["--model", args.model])
    if getattr(args, "effort", None):
        pane_command.extend(["--effort", args.effort])
    if getattr(args, "resume_thread_id", None):
        pane_command.extend(["--resume-thread-id", args.resume_thread_id])

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
            if item.get("clientId") == marker or marker in user_message_text(item):
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


async def wait_for_tui_thread(client, expected_thread_id, pane_id, timeout_seconds):
    """Return the idle thread the visible TUI loaded on this private app-server."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if pane_id is not None and not pane_exists(pane_id):
            raise RuntimeError("Codex TUI pane exited before loading its thread")
        remaining = max(0.0, deadline - time.monotonic())
        try:
            result = await asyncio.wait_for(
                client.request("thread/loaded/list", {}), timeout=remaining
            )
        except asyncio.TimeoutError as error:
            raise RuntimeError("Timed out asking Codex which TUI thread is loaded") from error
        loaded = result.get("data") or []
        if expected_thread_id:
            if expected_thread_id in loaded:
                return expected_thread_id
            if loaded:
                raise RuntimeError(
                    "Codex TUI loaded a different thread instead of "
                    f"{expected_thread_id}: {', '.join(loaded)}"
                )
        elif len(loaded) == 1:
            return loaded[0]
        elif len(loaded) > 1:
            raise RuntimeError(
                "Fresh Codex app-server loaded more than one thread: "
                + ", ".join(loaded)
            )
        await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    target = expected_thread_id or "a new thread"
    raise RuntimeError(f"Timed out waiting for Codex TUI to load {target}")


async def wait_for_mcp_startup(
    client, thread_id, cwd, pane_id, timeout_seconds, startup_log_path
):
    """Return after this TUI-owned thread reports every MCP startup outcome."""
    deadline = time.monotonic() + timeout_seconds
    if pane_id is not None and not pane_exists(pane_id):
        raise RuntimeError("Codex TUI pane exited before MCP status was ready")
    try:
        config_result = await asyncio.wait_for(
            client.request(
                "config/read", {"cwd": cwd, "includeLayers": False}
            ),
            timeout=max(0.0, deadline - time.monotonic()),
        )
    except asyncio.TimeoutError as error:
        raise RuntimeError("Timed out reading Codex MCP startup configuration") from error
    if pane_id is not None and not pane_exists(pane_id):
        raise RuntimeError("Codex TUI pane exited before MCP status was ready")

    # Codex 0.149.1's TUI defines its expected startup round from this same set
    # of enabled vendor servers. Require terminal notifications observed on
    # that TUI's connection before placing the Axis Brief in its queue.
    configured = (config_result.get("config") or {}).get("mcp_servers") or {}
    enabled_configured = {
        name
        for name, server in configured.items()
        if not isinstance(server, dict) or server.get("enabled", True)
    }
    unsettled = sorted(enabled_configured)
    while time.monotonic() < deadline:
        statuses = read_mcp_startup_statuses(startup_log_path, thread_id)
        unsettled = sorted(
            name
            for name in enabled_configured
            if (statuses.get(name) or {}).get("status")
            not in MCP_STARTUP_TERMINAL_STATES
        )
        if not unsettled:
            return {name: statuses[name] for name in sorted(enabled_configured)}
        if pane_id is not None and not pane_exists(pane_id):
            raise RuntimeError("Codex TUI pane exited before MCP status was ready")

        await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    raise RuntimeError(
        "Timed out waiting for Codex MCP startup (unsettled: "
        + ", ".join(unsettled)
        + ")"
    )


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


async def queue_review(client, thread_id, prompt, marker):
    """Durably queue one Axis Brief on the TUI-owned thread."""
    return await client.request(
        "thread/queue/add",
        {
            "threadId": thread_id,
            "clientUserMessageId": marker,
            "input": [
                {
                    "type": "text",
                    "text": prompt,
                    "text_elements": [],
                }
            ],
        },
    )


async def persist_and_queue_review(client, store, state, prompt, marker, timeout):
    """Make recovery discoverable before the durable queue accepts the Brief."""
    store.write(state["reviewSessionId"], state)
    return await asyncio.wait_for(
        queue_review(client, state["threadId"], prompt, marker),
        timeout=timeout,
    )


async def queued_review_present(client, thread_id, marker):
    """Search every durable queue page for one Bridge message marker."""
    cursor = None
    seen_cursors = set()
    while True:
        params = {"threadId": thread_id}
        if cursor is not None:
            params["cursor"] = cursor
        queue = await client.request("thread/queue/list", params)
        if any(
            submission.get("clientUserMessageId") == marker
            for submission in queue.get("data") or []
        ):
            return True
        cursor = queue.get("nextCursor")
        if cursor is None:
            return False
        if cursor in seen_cursors:
            raise AppServerError(
                "thread/queue/list repeated a pagination cursor"
            )
        seen_cursors.add(cursor)


def thread_has_active_turn(thread):
    status = thread.get("status") or {}
    if status.get("type") == "active":
        return True
    return any(
        turn.get("status") not in TERMINAL_TURN_STATUSES
        for turn in thread.get("turns") or []
    )


async def ensure_review_delivery(
    client, state, prompt, pane_id, timeout_seconds
):
    """Recover one recorded delivery without ever submitting its Brief twice.

    The durable queue and thread history are the evidence. An active turn whose
    user item is not visible yet is waited out; it is never treated as permission
    to enqueue another model turn.
    """
    deadline = time.monotonic() + timeout_seconds
    unreadable = None
    while time.monotonic() < deadline:
        if not pane_exists(pane_id):
            raise RuntimeError("Codex TUI pane exited before delivery was confirmed")
        if await queued_review_present(
            client, state["threadId"], state["marker"]
        ):
            return "queued"
        try:
            result = await client.request(
                "thread/read",
                {"threadId": state["threadId"], "includeTurns": True},
            )
        except AppServerError as error:
            unreadable = error
            result = None
        if result is not None:
            thread = result.get("thread") or {}
            if find_bridge_turn(thread, state["marker"]) is not None:
                return "started"
            if not thread_has_active_turn(thread):
                await queue_review(
                    client,
                    state["threadId"],
                    prompt,
                    state["marker"],
                )
                return "queued"
        await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    detail = f"; thread remained unreadable: {unreadable}" if unreadable else ""
    raise RuntimeError(
        "Timed out confirming Codex Brief delivery without risking a duplicate turn"
        + detail
    )


def make_runtime(prompt):
    """Returns the session's runtime directory, the request recorded beside its log.

    The Bridge retains prompt.txt for crash recovery; the TUI never receives it
    on its command line.
    """
    runtime_dir = pathlib.Path(tempfile.mkdtemp(prefix="claude-codex-tui-"))
    write_runtime_prompt(runtime_dir, prompt)
    return runtime_dir


def write_runtime_prompt(runtime_dir, prompt):
    """Atomically retain the current Brief beside its app-server runtime."""
    path = pathlib.Path(runtime_dir) / "prompt.txt"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(prompt, encoding="utf-8")
    os.replace(temporary, path)


def session_state(
    session_id, owner, runtime_dir, pane_id, thread_id, target, model, effort,
    marker, axis,
):
    now = time.time()
    return {
        "version": SESSION_STATE_VERSION,
        "reviewSessionId": session_id,
        "axis": axis,
        # Rounds this lineage has had, which the Rounds Contract caps.
        "rounds": SINGLE_ROUND,
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
    if status == "completed" and not has_report(final_message):
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


@dataclasses.dataclass(frozen=True)
class StoredAxisRun:
    """One axis whose reviewer is gone but whose report is still undelivered."""

    state: dict

    @property
    def thread_id(self):
        return self.state.get("threadId")


class AxisRunError(Exception):
    def __init__(self, state, thread_id, reason):
        super().__init__(reason)
        self.state = state
        self.thread_id = thread_id
        self.reason = reason


def axis_arguments(args, axis):
    """This call's arguments as one axis sees them, with that axis's choices pinned."""
    axis_args = argparse.Namespace(**vars(args))
    axis_args.axis = axis
    for choice in ("model", "effort"):
        setattr(axis_args, choice, resolve_axis_choice(args, axis, choice))
    return axis_args


def launch_axis(args, brief, tmux_target, split_direction):
    axis_args = axis_arguments(args, brief.axis)
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
        thread_id = await wait_for_tui_thread(
            client, None, pane_id, args.startup_timeout
        )
        await wait_for_mcp_startup(
            client,
            thread_id,
            args.cwd,
            pane_id,
            args.startup_timeout,
            runtime_dir / MCP_STARTUP_LOG_FILENAME,
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
        # The TUI is already attached to an idle thread. The record goes down
        # before the durable queue receives the Brief, so either side of a
        # killed driver remains discoverable without a second submission.
        await persist_and_queue_review(
            client,
            store,
            state,
            launch.prompt,
            launch.marker,
            args.startup_timeout,
        )
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
        return await drive_new_review(launch, owner, store)
    except AxisRunError as error:
        return AxisFailure(error.state, error.thread_id, error.reason)


def undelivered_report(state):
    report = state.get("report")
    if isinstance(report, dict) and report.get("delivered") is False:
        return report
    return None


def result_from_report(report):
    result = {
        name: report[name]
        for name in ("status", "finalMessage", "reviewSessionId")
    }
    # A record stored before reports had files of their own names no file.
    result["reportFile"] = report.get("reportFile")
    if "reason" in report:
        result["reason"] = report["reason"]
    return result


def cost_from_report(report):
    saved = report["costCounters"]
    counters = (
        dict(saved)
        if all(saved.get(name) is not None for name in COUNTERS)
        else None
    )
    return counters, report.get("resolvedModel"), report.get("costDetail")


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
    args.resume_thread_id = state["threadId"]
    try:
        pane_id = open_child_pane(args, runtime_dir)
    except Exception:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        raise

    client = None
    try:
        client = await connect_when_ready(
            runtime_dir / "app-server.sock",
            pane_id,
            args.startup_timeout,
            runtime_dir / "app-server.log",
        )
        await wait_for_tui_thread(
            client, state["threadId"], pane_id, args.startup_timeout
        )
        await wait_for_mcp_startup(
            client,
            state["threadId"],
            args.cwd,
            pane_id,
            args.startup_timeout,
            runtime_dir / MCP_STARTUP_LOG_FILENAME,
        )
        # The record names the TUI-owned pane before its durable queue receives
        # the follow-up, so recovery can distinguish and complete either gap.
        state["runtimeDir"] = str(runtime_dir)
        state["socketPath"] = str(runtime_dir / "app-server.sock")
        state["paneId"] = pane_id
        state["updatedAt"] = time.time()
        await persist_and_queue_review(
            client, store, state, prompt, marker, args.startup_timeout
        )
        return await wait_for_review(
            client,
            state["threadId"],
            marker,
            pane_id,
            args.timeout,
        )
    except Exception:
        # A returned timeout is an ordinary failed round, so #26 requires its
        # pane to settle. A killed driver raises outside Exception and leaves
        # the same pane/runtime intact for --recover-session.
        cleanup_pane(pane_id, runtime_dir)
        raise
    finally:
        if client is not None:
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
                write_runtime_prompt(state["runtimeDir"], prompt)
                await persist_and_queue_review(
                    client,
                    store,
                    state,
                    prompt,
                    marker,
                    args.startup_timeout,
                )
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
    """Recover every live reviewer or undelivered report this owner has.

    Returns each completed or failed axis for the sessions this owner already owns.

    The caller reaching here has lost the handle its driver was going to print —
    the driver was killed, or its output never arrived. This starts no pane and
    no thread or duplicate turn: durable queue and thread history decide whether
    a live record still needs delivery, while a stored report is handed back
    without its reviewer. With none of those it raises
    `NoLiveSessionError` rather than falling back to a first review.
    """
    async def recover(state):
        if undelivered_report(state) is not None:
            return StoredAxisRun(state)
        if any(not state.get(field) for field in CodexLane.RECOVERABLE_FIELDS):
            return None
        client = await connect_existing_session(state)
        if client is None:
            return None
        try:
            try:
                prompt = (
                    pathlib.Path(state["runtimeDir"]) / "prompt.txt"
                ).read_text(encoding="utf-8")
                await ensure_review_delivery(
                    client,
                    state,
                    prompt,
                    state["paneId"],
                    args.startup_timeout,
                )
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
        *(
            recover(state)
            for state in store.find_by_owner(owner)
        )
    )
    recoverable = [run for run in recovered if run is not None]
    if recoverable:
        return {run.state["axis"]: run for run in recoverable}
    raise NoLiveSessionError(
        "No live review session for this tmux pane and worktree. "
        "Nothing to recover; start a review instead."
    )


class Lane:
    """The seam every Lane is reached through, and the half every Lane shares.

    Preparation is finished by the time a Lane is opened, so a Lane takes one
    Axis Brief and gives back that axis's result; everything that is a vendor's
    rather than the review's lives on the far side of this seam. What every Lane
    does alike — whose sessions these are, how an axis ends, what a recovered
    review was prepared from — is here; what only one vendor does is not.
    """

    #: Whether this Lane's reviewer runs in a tmux pane of the caller's server.
    NEEDS_TMUX = False
    #: What an axis that reached no reviewer of its own spent, which is nothing
    #: readable. Each Lane names the reason its own reviewer could not be read.
    NO_COST = (None, None, "this axis reached no reviewer to read a cost from")

    def __init__(self, args, owner, store):
        self.args = args
        self.owner = owner
        self.store = store

    def axis_cost(self, run):
        """What one axis spent, read wherever this Lane's reviewer records it."""
        raise NotImplementedError

    def store_report(self, run, result):
        """Persist one report and its hook facts before recovery is dismantled.

        Returns the file this axis's report is readable in, or None where there
        was no report to write. The store owns where its files live, so this is
        the only place the path is composed and no caller resolves it again.
        """
        if run.state is None:
            return None
        counters, model, detail = self.axis_cost(run)
        report_file = self.store.write_report(
            run.state["reviewSessionId"], result["finalMessage"]
        )
        report = dict(result)
        report.update({
            "reportFile": report_file,
            "resolvedModel": model,
            "costCounters": {
                name: counters.get(name) if counters is not None else None
                for name in COUNTERS
            },
            "costDetail": detail,
            "delivered": False,
        })
        run.state["report"] = report
        run.state["updatedAt"] = time.time()
        self.store.write(run.state["reviewSessionId"], run.state)
        return report_file

    def settle_result(self, run):
        """Return a stored report, or persist the result still held by its Lane."""
        report = undelivered_report(run.state) if run.state else None
        if report is not None:
            return result_from_report(report)
        result = self.result_for(run)
        # Every axis names its report file, and an axis that wrote none says so,
        # the way every axis names its session and a sessionless one says None.
        result["reportFile"] = self.store_report(run, result)
        return result

    def mark_delivered(self, run):
        """Mark a report only after its JSON has been printed to the caller."""
        if run.state is None:
            return
        report = undelivered_report(run.state)
        if report is None:
            return
        report["delivered"] = True
        run.state["updatedAt"] = time.time()
        self.store.write(run.state["reviewSessionId"], run.state)

    def end_axis(self, axis, result, run):
        """End an axis this Lane drove to a result of its own."""
        hook_axis_end(
            self.args,
            axis,
            result["status"],
            result["reviewSessionId"],
            result["reportFile"],
            self.axis_cost(run),
        )

    def end_launched_axis(self, axis):
        """End an axis that opened its reviewer and never got a result of its own.

        A sibling axis failing to open tears this one down before it could report
        anything, and the point still owes the caller one end for the child it
        was told about.
        """
        hook_axis_end(self.args, axis, FAILED_STATUS, None, None, self.NO_COST)

    def recovered_preparation(self, runs):
        """What a recovered review was prepared from, read back off its own records."""
        preparation = next(
            (
                run.state.get("preparation")
                for run in runs
                if run.state is not None
            ),
            None,
        )
        if preparation is None:
            return None
        # A record written before the Bridge wrote spec files names none, which
        # is the truth about that review rather than a gap in its receipt. The
        # field is filled in so every result carries it and no caller reading
        # the JSON has to handle two shapes (#33).
        return {**preparation, "specFile": preparation.get("specFile")}


class CodexLane(Lane):
    """Delivery to the codex reviewer: one Codex TUI pane per axis.

    Everything that is codex's rather than the review's — panes, threads,
    app-server connections, the record on disk — lives on this side of the seam.
    """

    name = REVIEWER_CODEX
    NEEDS_TMUX = True
    NO_COST = (None, None, NO_THREAD_DETAIL)
    #: What a record must carry for a recovering caller to find its turn again.
    RECOVERABLE_FIELDS = ("marker", "threadId")

    def __init__(self, args, owner, store):
        super().__init__(args, owner, store)
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
        run = await drive_terminal_axis(launch, self.owner, self.store)
        self.settle_result(run)
        cleanup_pane(launch.pane_id, launch.runtime_dir)
        return run

    async def resume(self, brief):
        """Put one more turn to the lineage a resume handle names."""
        return await run_existing_review(self.args, brief, self.owner, self.store)

    async def recover(self):
        """Recover every live reviewer or undelivered report this owner has."""
        return await run_recovered_reviews(self.args, self.owner, self.store)

    def settle(self, run):
        """Close one axis out: its record written, its pane down, its result returned."""
        result = self.settle_result(run)
        if self.args.recover_session or self.args.resume_session:
            cleanup_pane(
                run.state.get("paneId") if run.state else None,
                run.state.get("runtimeDir") if run.state else None,
            )
        return result

    def result_for(self, run):
        """Build the shared axis result while its source is still available."""
        args = self.args
        if isinstance(run, AxisFailure):
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
        return axis_result(state, turn, final_message)

    def axis_cost(self, run):
        """What an axis spent, read from the Codex rollout its own thread wrote."""
        report = undelivered_report(run.state) if run.state else None
        if report is not None:
            return cost_from_report(report)
        if run.thread_id is None:
            return self.NO_COST
        return harvest_rollout(codex_sessions_root(), run.thread_id)


# A headless reviewer has nobody to answer a permission prompt, so the mode is
# not a caller-tunable option.
CLAUDE_PERMISSION_MODE = "bypassPermissions"
# Which Claude login the reviewer spends on. Claude Code scopes login state to
# this variable, so setting it is what routes the spend. The value arrives
# already resolved to a profile directory: this bridge reads no account registry.
CLAUDE_CONFIG_HOME_ENV_VAR = "CLAUDE_CONFIG_DIR"
CLAUDE_BINARY_ENV_VAR = "CODE_REVIEW_CLAUDE_BINARY"
# The one JSON object a headless reviewer prints, and everything else it said.
CLAUDE_RESULT_FILENAME = "result.json"
CLAUDE_LOG_FILENAME = "claude.log"
# What a headless result's `usage` object calls each of the four counters. Claude
# reports its cached tokens beside the input count rather than inside it, so the
# four arrive already disjoint.
CLAUDE_USAGE_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}
NO_RESULT_DETAIL = "this axis returned no result to read a cost from"
NO_USAGE_DETAIL = "this axis's result reported no usage to read a cost from"
# How often a recovering caller looks to see whether the reviewer it adopted has
# exited. It did not start that process, so waiting on it is polling or nothing.
CLAUDE_EXIT_POLL_SECONDS = 0.25


def resolve_claude_binary(explicit=None, environment=None):
    """Absolute path of the real `claude` executable.

    The launch below is an argv list with no shell, so a `claude` *shell
    function* is already out of the picture. Resolving symlinks as well means a
    wrapper script dropped on PATH cannot intercept the reviewer either.
    """
    environment = os.environ if environment is None else environment
    candidate = explicit or environment.get(CLAUDE_BINARY_ENV_VAR)
    if not candidate:
        candidate = shutil.which("claude", path=environment.get("PATH"))
    if not candidate:
        raise RuntimeError("Cannot find the claude executable")
    resolved = pathlib.Path(candidate).resolve()
    if not resolved.is_file():
        raise RuntimeError(f"claude executable does not exist: {resolved}")
    return str(resolved)


def build_claude_command(binary, prompt, model, effort, resume_id):
    """The exact argv handed to one headless reviewer, in a stable order."""
    command = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        CLAUDE_PERMISSION_MODE,
    ]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    if resume_id:
        command.extend(["-r", resume_id])
    command.append(prompt)
    return command


def claude_child_environment(account):
    """The reviewer's environment, or `None` to inherit the caller's untouched.

    An account is a profile directory, and naming one overrides whatever login
    the caller happens to be on, so a review billed to one account is not a hole
    in it. A call that names none inherits the caller's login exactly as it
    stands: the default home spelled out explicitly is a login that can fail
    where the inherited one works.
    """
    if not account:
        return None
    return {**os.environ, CLAUDE_CONFIG_HOME_ENV_VAR: account}


def claude_process_alive(pid):
    """True while the reviewer a record names is still running."""
    return process_exists(pid)


def terminate_claude(pid):
    """Take down a reviewer and whatever it started, and fail at nothing."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        return


class ClaudeProcess:
    """One headless reviewer, seen only through what its driver may do to it."""

    def __init__(self, process):
        self.process = process

    @property
    def pid(self):
        return self.process.pid

    async def wait(self, timeout):
        await asyncio.wait_for(asyncio.to_thread(self.process.wait), timeout)

    def kill(self):
        terminate_claude(self.pid)


def launch_claude(args, command, runtime_dir):
    """Start this axis's reviewer, in a session of its own.

    A session of its own is what makes this Lane recoverable: a driver that dies
    leaves the reviewer running and its result still on its way to the file it
    prints into, which is the promise the codex Lane's pane makes too.
    """
    result_path = runtime_dir / CLAUDE_RESULT_FILENAME
    log_path = runtime_dir / CLAUDE_LOG_FILENAME
    try:
        with open(result_path, "wb") as printed, open(log_path, "wb") as said:
            process = subprocess.Popen(
                command,
                cwd=args.cwd,
                env=claude_child_environment(args.account),
                stdin=subprocess.DEVNULL,
                stdout=printed,
                stderr=said,
                start_new_session=True,
            )
    except OSError as error:
        raise RuntimeError(f"Cannot launch headless Claude: {error}") from error
    return ClaudeProcess(process)


def open_child_process(args, command, runtime_dir):
    """Start this axis's reviewer, and tell the caller's hook which child it got."""
    process = launch_claude(args, command, runtime_dir)
    hook_child_launch(args)
    return process


@dataclasses.dataclass(frozen=True)
class ClaudeResult:
    """The one JSON object a headless reviewer prints, in the parts a Lane reads."""

    session_id: str
    result: str
    is_error: bool
    subtype: str
    permission_denials: list
    payload: dict


def read_claude_result(path):
    """What the reviewer printed, or the failure that it printed no result."""
    try:
        text = pathlib.Path(path).read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError as error:
        raise RuntimeError(
            f"the reviewer's result could not be read: {error}"
        ) from error
    if not text:
        raise RuntimeError("the reviewer produced no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"the reviewer's output is not JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError("the reviewer's output is not a JSON object")
    if not payload.get("session_id"):
        raise RuntimeError("the reviewer's output carries no session id")
    denials = payload.get("permission_denials") or []
    if not isinstance(denials, list):
        denials = [denials]
    return ClaudeResult(
        session_id=payload["session_id"],
        result=payload.get("result") or "",
        is_error=bool(payload.get("is_error")),
        subtype=payload.get("subtype") or "",
        permission_denials=denials,
        payload=payload,
    )


def claude_usage(payload):
    """The four disjoint counters one axis billed, or `None` when it reported none.

    All four or nothing: a `usage` object missing one of them is not a partial
    answer, it is a shape this bridge does not recognise, and billing an axis for
    three of its four counters would understate it without saying so.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    counters = {}
    for name, field in CLAUDE_USAGE_FIELDS.items():
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        counters[name] = value
    return counters


def claude_resolved_model(payload):
    """The model one axis ran on, or `None` when its result named no single one.

    The per-model breakdown is keyed by the model that was billed, so an alias
    like `opus` is resolved to the id behind it. More than one key is an axis
    that ran on more than one model, which no single id describes.
    """
    billed = payload.get("modelUsage")
    if isinstance(billed, dict) and len(billed) == 1:
        return next(iter(billed))
    return None


def claude_axis_cost(parsed):
    """What one axis spent, read from the result its own reviewer printed."""
    counters = claude_usage(parsed.payload)
    model = claude_resolved_model(parsed.payload)
    return counters, model, None if counters else NO_USAGE_DETAIL


def claude_failure_reason(parsed):
    """Why this axis's report is not one, or `None` when it is."""
    if parsed.is_error:
        return f"the reviewer ended in error: {parsed.subtype or 'unknown'}"
    if not has_report(parsed.result):
        return "review completed without a final message"
    if parsed.permission_denials:
        return (
            f"the reviewer was denied {len(parsed.permission_denials)} "
            "permission(s), so its report covers less than it was asked to review"
        )
    return None


def claude_axis_result(state, parsed):
    """One axis's result, in the shape every Lane returns it."""
    reason = claude_failure_reason(parsed)
    result = {
        "status": FAILED_STATUS if reason else "completed",
        "finalMessage": parsed.result,
        "reviewSessionId": state["reviewSessionId"],
    }
    if reason:
        result["reason"] = reason
    return result


def headless_session_state(
    session_id, owner, runtime_dir, pid, target, model, effort, axis
):
    """The record one headless axis leaves: the reviewer, and where it prints."""
    now = time.time()
    return {
        "version": SESSION_STATE_VERSION,
        "reviewSessionId": session_id,
        "axis": axis,
        # Rounds this lineage has had, which the Rounds Contract caps.
        "rounds": SINGLE_ROUND,
        "owner": owner.to_dict(),
        "runtimeDir": str(runtime_dir),
        "resultPath": str(pathlib.Path(runtime_dir) / CLAUDE_RESULT_FILENAME),
        "pid": pid,
        # The lineage this axis's next round resumes; it has none until the
        # reviewer has printed the session it ran under.
        "claudeSessionId": None,
        "target": target,
        "model": model,
        "effort": effort,
        "createdAt": now,
        "updatedAt": now,
    }


def result_awaits(path):
    """True when a reviewer that is no longer running left a result behind."""
    try:
        return pathlib.Path(path).stat().st_size > 0
    except OSError:
        return False


async def wait_for_claude_exit(pid, timeout):
    """Wait out a reviewer this caller never started, which is polling or nothing."""
    deadline = time.monotonic() + timeout
    while claude_process_alive(pid):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"the reviewer was still running after {timeout}s"
            )
        await asyncio.sleep(CLAUDE_EXIT_POLL_SECONDS)


@dataclasses.dataclass(frozen=True)
class ClaudeLaunch:
    """One axis's reviewer, started, recorded, and not yet waited on."""

    args: argparse.Namespace
    brief: AxisBrief
    command: list
    runtime_dir: pathlib.Path
    process: object
    state: dict


@dataclasses.dataclass(frozen=True)
class ClaudeRun:
    """One axis this Lane drove, whether or not it reached a report."""

    state: dict
    parsed: ClaudeResult | None = None
    reason: str = ""


class ClaudeLane(Lane):
    """Delivery to the claude reviewer: one headless process per axis.

    There is no pane and no thread on this side of the seam: `claude -p` prints
    one JSON object and exits, so what this Lane owns is a process, the file that
    object lands in, and the record naming both. A review here needs no tmux,
    which is this Lane's dependency rather than a difference in the review
    (ADR-0003).
    """

    name = REVIEWER_CLAUDE
    NO_COST = (None, None, NO_RESULT_DETAIL)
    #: What a record must carry for a recovering caller to reach its reviewer.
    RECOVERABLE_FIELDS = ("pid", "resultPath")
    NO_LIVE_SESSION = (
        "No live review session for this worktree. "
        "Nothing to recover; start a review instead."
    )

    def open(self, brief):
        """Start one axis's reviewer, recorded and ready to be waited on.

        The record goes down here rather than where the waiting starts, because
        the reviewer is already running by the time this returns: a driver killed
        in between would otherwise leave a live reviewer that `--recover-session`
        can no longer find.
        """
        axis_args = axis_arguments(self.args, brief.axis)
        launch = self.launch(
            axis_args, brief, axis_args.model, axis_args.effort, None
        )
        state = headless_session_state(
            str(uuid.uuid4()),
            self.owner,
            launch.runtime_dir,
            launch.process.pid,
            self.args.base,
            axis_args.model,
            axis_args.effort,
            brief.axis,
        )
        state["preparation"] = preparation_report(self.args)
        self.store.write(state["reviewSessionId"], state)
        return dataclasses.replace(launch, state=state)

    def launch(self, axis_args, brief, model, effort, resume_id):
        """Put one brief to a reviewer of this axis's own, new lineage or resumed."""
        command = build_claude_command(
            resolve_claude_binary(self.args.claude_binary),
            brief.text,
            model,
            effort,
            resume_id,
        )
        runtime_dir = make_runtime(brief.text)
        try:
            process = open_child_process(axis_args, command, runtime_dir)
        except Exception:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            raise
        return ClaudeLaunch(
            axis_args, brief, command, runtime_dir, process, None
        )

    def discard(self, launch):
        """Tear down an axis that started but will never be waited on.

        Its record goes with it: a review that never began is not one a later
        caller should find a handle to.
        """
        launch.process.kill()
        if launch.state is not None:
            self.store.remove(launch.state["reviewSessionId"])
        shutil.rmtree(launch.runtime_dir, ignore_errors=True)

    async def deliver(self, launch):
        """Drive one started axis to a result of its own."""
        run = await self.drive(launch, launch.state)
        self.settle_result(run)
        return run

    async def drive(self, launch, state):
        """Wait this axis's reviewer out and read what it printed.

        Nothing here throws the reviewer's output away. The caller persists it
        in the record as soon as this returns, before the runtime file is removed.
        """
        try:
            await launch.process.wait(self.args.timeout)
            parsed = read_claude_result(
                launch.runtime_dir / CLAUDE_RESULT_FILENAME
            )
        except TimeoutError:
            # Nothing else will wait on this reviewer, so it is stopped rather
            # than left running at the caller's expense.
            launch.process.kill()
            return ClaudeRun(
                state,
                reason=(
                    f"the reviewer did not finish within {self.args.timeout}s"
                ),
            )
        except Exception as error:
            return ClaudeRun(state, reason=str(error) or type(error).__name__)
        return ClaudeRun(state, parsed=parsed)

    async def resume(self, brief):
        """Put one more turn to the lineage a resume handle names."""
        state = self.args.resume_state
        if state is None:
            state = self.store.read(self.args.resume_session)
        validate_resume_axis(self.args, state)
        validate_session_owner(state, self.owner)
        apply_session_model_choice(self.args, state)
        launch = self.launch(
            axis_arguments(self.args, state["axis"]),
            brief,
            state["model"],
            state["effort"],
            state.get("claudeSessionId"),
        )
        state["runtimeDir"] = str(launch.runtime_dir)
        state["resultPath"] = str(
            launch.runtime_dir / CLAUDE_RESULT_FILENAME
        )
        state["pid"] = launch.process.pid
        state["updatedAt"] = time.time()
        # On disk before the turn is awaited, for the same reason a first review
        # writes its record early: a driver killed mid-review must leave a
        # reviewer the recovery path can still find.
        self.store.write(state["reviewSessionId"], state)
        return await self.drive(dataclasses.replace(launch, state=state), state)

    def recoverable(self):
        """Every live reviewer or undelivered report this owner can recover.

        A reviewer outlives its driver, so what survives that death is the
        record, the process, and the file the result lands in. Once the report
        is stored, those runtime dependencies may be gone; until then, a reviewer
        that exited without printing anything leaves nothing to wait on.
        """
        return [
            state
            for state in self.store.find_by_owner(self.owner)
            if undelivered_report(state) is not None
            or (
                all(state.get(field) for field in self.RECOVERABLE_FIELDS)
                and pathlib.Path(state["runtimeDir"]).is_dir()
                and (
                    claude_process_alive(state["pid"])
                    or result_awaits(state["resultPath"])
                )
            )
        ]

    async def recover(self):
        """Recover every live reviewer or undelivered report this owner has.

        The caller reaching here has lost the handle its driver was going to
        print. This starts no reviewer: it waits on live reviewers and hands
        stored reports back directly. With neither it raises `NoLiveSessionError`
        rather than falling back to a first review.
        """
        states = self.recoverable()
        if not states:
            raise NoLiveSessionError(self.NO_LIVE_SESSION)
        runs = await asyncio.gather(*(self.rejoin(state) for state in states))
        return {run.state["axis"]: run for run in runs}

    async def rejoin(self, state):
        """Return one stored report, or wait out the reviewer this call adopted."""
        if undelivered_report(state) is not None:
            return StoredAxisRun(state)
        try:
            await wait_for_claude_exit(state["pid"], self.args.timeout)
            parsed = read_claude_result(state["resultPath"])
        except TimeoutError as error:
            # This call adopted the reviewer and is now giving up on it, and
            # nobody else is coming: settling this axis is what makes its record
            # unrecoverable, so the reviewer must not outlive it.
            terminate_claude(state["pid"])
            return ClaudeRun(state, reason=str(error))
        except Exception as error:
            return ClaudeRun(state, reason=str(error) or type(error).__name__)
        return ClaudeRun(state, parsed=parsed)

    def settle(self, run):
        """Persist one axis's report, then release its runtime files."""
        state = run.state
        result = self.settle_result(run)
        self.let_go_of_runtime(state)
        return result

    def result_for(self, run):
        """Build the shared axis result while its source is still available."""
        state = run.state
        if run.parsed is None:
            return {
                "status": FAILED_STATUS,
                "finalMessage": "",
                "reviewSessionId": state["reviewSessionId"],
                "reason": run.reason,
            }
        state["claudeSessionId"] = run.parsed.session_id
        state["updatedAt"] = time.time()
        if not self.args.recover_session:
            state["target"] = self.args.base
            state["preparation"] = preparation_report(self.args)
        return claude_axis_result(state, run.parsed)

    def let_go_of_runtime(self, state):
        """Drop what this axis's reviewer left on disk, now nothing needs it."""
        shutil.rmtree(state["runtimeDir"], ignore_errors=True)

    def axis_cost(self, run):
        """What an axis spent, read from the result its own reviewer printed."""
        report = undelivered_report(run.state)
        if report is not None:
            return cost_from_report(report)
        if run.parsed is None:
            return self.NO_COST
        return claude_axis_cost(run.parsed)


# Every reviewing vendor there is a Lane for, keyed by the name `--reviewer`
# takes. The keys are the argument's accepted values, so a Lane cannot be
# reachable by a name the command line rejects, or rejected by one it accepts.
LANES = {CodexLane.name: CodexLane, ClaudeLane.name: ClaudeLane}


def resolve_lane(args, store):
    """The Lane this call named, under the owner identity it runs as.

    Both are resolved before any of the Lane opens, so a reviewer there is no
    Lane for, and a Lane whose dependencies this caller has not got, are each
    refused by name rather than part way through a review.
    """
    lane = LANES.get(args.reviewer)
    if lane is None:
        known = ", ".join(sorted(LANES))
        raise RuntimeError(
            f"Unknown reviewer for --reviewer: {args.reviewer!r}; "
            f"known reviewers: {known}"
        )
    return lane(args, lane_owner(args, lane), store)


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
            lane.end_launched_axis(brief.axis)
        raise
    runs = await asyncio.gather(*(lane.deliver(launch) for launch in launches))
    return {brief.axis: run for brief, run in zip(briefs, runs, strict=True)}


def report_refusal(args, refusal):
    """Give the caller the refusal and nothing else; no Lane opened."""
    print(json.dumps(refusal, ensure_ascii=False))
    args.status = refusal["status"]
    return 1


async def run_bridge(args):
    if not pathlib.Path(args.cwd).is_dir():
        raise RuntimeError(f"Working directory does not exist: {args.cwd}")
    store = SessionStore()
    # Resolved before a resumed handle is looked at, because whose session it is
    # decides whether this caller may be told anything about it at all.
    lane = resolve_lane(args, store)
    refusal = refuse_resume_past_cap(args, lane.owner, store)
    if refusal is not None:
        return report_refusal(args, refusal)
    probe = args.probe or args.browser_probe
    if not args.recover_session and not probe:
        args.scope = resolve_review_scope(args.cwd, args.base)
        ensure_scope_holds_work(args.cwd, args.scope)
        args.preparation = prepare_review(args, store)
    elif probe:
        args.preparation = None
    # Preparation has succeeded and no Lane has opened yet, which is what this
    # point promises: a review that failed before here never started.
    hook_review_start(args, review_start_model(args, args.resume_state))
    # The lock stays process-scoped: it serialises concurrent calls from one
    # pane, and a driver that dies releases it. Duplicate prevention across a
    # driver's death is the recovery path's job, not a longer-lived lock's — a
    # lock that outlived its holder would also have to be reaped, and would
    # block the very recovery call that clears the duplicate.
    with owner_lock(store, lane.owner):
        if args.recover_session:
            runs = await lane.recover()
        elif args.resume_session:
            # The binding answer, and the only one that spends a round: the
            # record is re-read and written back here, where the lock keeps two
            # callers from taking the same round.
            args.resume_state, refusal = grant_round(args, lane.owner, store)
            if refusal is not None:
                return report_refusal(args, refusal)
            runs = {args.axis: await lane.resume(axis_brief(args, args.axis))}
        else:
            runs = await deliver_briefs(args, lane, axis_briefs(args))

        results = {}
        for axis, run in runs.items():
            result = lane.settle(run)
            # Named here rather than in either Lane: what a caller may do next
            # is the review's answer, and both Lanes give the same one.
            result["next"] = next_action(axis, rounds_had(run.state))
            if args.recover_session:
                result["recovered"] = True
            results[axis] = result
        # Every report and its cost facts are now durable, so the hook can use
        # the same values whether this is the original or a recovered delivery.
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
        print(json.dumps(output, ensure_ascii=False), flush=True)
        for run in runs.values():
            lane.mark_delivered(run)
        args.status = output["status"]
        succeeded = output["status"] == "completed"
        return 0 if succeeded else 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Launch or resume a code review on the Lane --reviewer names."
    )
    parser.add_argument(
        "--reviewer",
        required=True,
        choices=tuple(LANES),
        help="reviewing vendor this review is delivered to",
    )
    parser.add_argument(
        "--base", help="fixed point the Review Scope runs from"
    )
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
        "--model",
        help="model for this review lineage (default: the reviewer's own config)",
    )
    parser.add_argument(
        "--effort",
        help="reasoning effort for this review lineage (default: the reviewer's "
             "own config)",
    )
    parser.add_argument(
        "--standards-model",
        help="model for the standards axis (default: --model)",
    )
    parser.add_argument(
        "--standards-effort",
        help="reasoning effort for the standards axis (default: --effort)",
    )
    parser.add_argument(
        "--spec-model",
        help="model for the spec axis (default: --model)",
    )
    parser.add_argument(
        "--spec-effort",
        help="reasoning effort for the spec axis (default: --effort)",
    )
    parser.add_argument("--network", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--account",
        help="profile directory the reviewer is launched under, on a Lane whose "
             "reviewer has one; without it the reviewer inherits the caller's "
             "own login",
    )
    parser.add_argument("--resume-session")
    parser.add_argument(
        "--recover-session",
        action="store_true",
        help=(
            "recover a live reviewer or undelivered report this tmux pane and "
            f"worktree already own, instead of starting one (exit "
            f"{NO_LIVE_SESSION_EXIT} when there is none)"
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
    parser.add_argument("--claude-binary", help=argparse.SUPPRESS)
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
    pane_parser.add_argument("--parent-pid", type=int, required=True)
    pane_parser.add_argument("--resume-thread-id")
    pane_parser.add_argument("--network", action="store_true")
    pane_parser.add_argument("--model")
    pane_parser.add_argument("--effort")
    return pane_parser


def build_tui_proxy_parser():
    proxy_parser = argparse.ArgumentParser(add_help=False)
    proxy_parser.add_argument("--upstream-socket", required=True)
    proxy_parser.add_argument("--listen-socket", required=True)
    proxy_parser.add_argument("--startup-log", required=True)
    return proxy_parser


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_pane":
        args = build_pane_parser().parse_args(sys.argv[2:])
        return run_pane(args)
    if len(sys.argv) > 1 and sys.argv[1] == "_tui_proxy":
        args = build_tui_proxy_parser().parse_args(sys.argv[2:])
        asyncio.run(run_tui_proxy(args))
        return 0
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
