# review-switch

Review-Switch runs one code review protocol and delivers it to the reviewing vendor you name:
`claude` or `codex`. The review runs outside the session that asked for it, on both Lanes.

`review-bridge` is the review. It pins the Review Scope — the fixed point to your working tree as
it stands, committed or not — fetches the spec you name, gathers the standards sources, fills one
Axis Brief per axis, and delivers each to its Lane. `--axis both` reviews Standards and Spec
concurrently and returns the two reports separately.

## Install

```bash
git clone https://github.com/okqixiaobao727-design/review-switch.git
cd review-switch
mkdir -p "$HOME/.claude/skills" "$HOME/.local/bin"
ln -sfn "$PWD/skills/review-switch" "$HOME/.claude/skills/review-switch"
ln -sfn "$PWD/bridge/review_bridge.py" "$HOME/.local/bin/review-bridge"
```

The second link puts the Bridge on your `PATH` as `review-bridge`; use any directory on your
`PATH` if `~/.local/bin` is not on yours.

Register `hook/review-adjudicator.sh` as a Claude Code `PreToolUse` hook matching `Skill`.

## Running a review

From the repository being reviewed:

```bash
review-bridge --reviewer codex --base main --spec '#42' --axis both
```

It prints one JSON object: a `preparation` receipt, and one entry per axis under `axes` carrying
that axis's `status`, `finalMessage`, `reviewSessionId`, `reportFile` — a markdown file holding
that axis's report, or `null` where it produced none — and `next`, the one action you are
permitted after that result. `--model` and `--effort` pin the whole review;
`--standards-model`, `--standards-effort`, `--spec-model`, and `--spec-effort` pin one axis at a
time. Omit them and the vendor's own configuration applies. `--recover-session` re-attaches to a
review whose driver died, and exits `3` when no live review belongs here. `--help` lists every
option.

The Bridge reads no configuration file of its own, so a review resolves the same way on every
machine it is invoked from.

## The round cap

One lineage gets one standards pass and at most one spec re-review, scoped to the fixes the
findings required. The Bridge holds that cap: it refuses a resume past it and reports `escalate`
as the next permitted action. What escalation *is* is yours — a fresh review is always available.

## Lifecycle Hooks

A caller that wants a review observed hands in the commands to run: `--on-child-launch`,
`--on-review-start`, `--on-axis-end`, and `--on-review-end`, each one command string. Each runs
once in the reviewed working directory, with that point's facts in its environment as `REVIEW_*`
variables. Pass none and nothing extra runs; a command that fails, hangs, or is missing leaves
the review's result untouched.

## Asking from inside a Claude session

`/review-switch` is the Dispatcher, and the only skill this project installs. It resolves the
Lane this machine is configured for, completes the fixed point and the spec reference, and calls
`review-bridge` — the same command a terminal runs.

Two files configure it, and the Dispatcher is the only thing that reads either:

- `~/.claude/code-reviewer` — the exact value `codex` selects the codex Lane; any other value or
  a missing file selects the claude Lane. A Lane you name when you ask overrides it.
- `~/.claude/review-hooks/` — one file per lifecycle point, named `child-launch`,
  `review-start`, `axis-end`, or `review-end`, each holding one command. A missing directory,
  missing file, or blank file leaves that point unset.

## Dependencies

Both Lanes need Python 3.11+ with `aiohttp` (`pip install aiohttp`) installed for the interpreter
that runs `review-bridge`, and `git`.

- **codex Lane** — the `codex` CLI, and `tmux`: each axis is an interactive TUI lineage in a pane
  of its own. The TUI creates or resumes an idle thread; the Bridge observes that thread's MCP
  startup on the same TUI connection, records recovery state, and queues the Axis Brief only after
  startup settles. The pane is torn down when the turn ends and the lineage is left resumable.
- **claude Lane** — the `claude` CLI. Each axis is a headless process, and no tmux is involved.

`code-review-graph` is optional on either Lane: when its CLI is available, the Bridge adds
navigation pointers to each Axis Brief. It points the CLI at the checkout under review — a
linked worktree as readily as a main checkout — and builds the graph there when none exists,
so a first review in a fresh worktree takes a few seconds longer than later ones. The review
still runs without it, and any failure of the tool is simply the tool being absent.

Running the test suites additionally needs `pytest`.
