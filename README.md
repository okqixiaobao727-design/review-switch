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

A spec reference that is an issue number or issue URL is resolved with the `gh` CLI, so an
issue-backed spec needs a GitHub remote that `gh` can resolve. A repository using any other
tracker passes a path to a spec file instead — for example, `--spec docs/spec.md`, or an absolute
path. The Bridge reads that file where it lies and never copies it. If an issue reference cannot
be fetched, the review still runs with a weaker Spec axis: `preparation.specSource` reads
`not fetched: <reference>`, and the axis is told the spec was unreachable and reports exactly
that rather than inferring requirements from the diff.

It prints one JSON object: a `preparation` receipt, and one entry per axis under `axes` carrying
that axis's `status`, `finalMessage`, `reviewSessionId`, `reportFile` — a markdown file holding
that axis's report, or `null` where it produced none — and `next`, the one action you are
permitted after that result: `fix and stop`, `fix then one re-review`, `run again`, or `escalate`.
Every axis also carries `nextCall`: the exact Bridge argv for a permitted re-review or fresh
single-axis run, including the Response file and line shape for a re-review, or `null` when no
Bridge call is permitted. `--model` and `--effort` pin the whole review;
`--standards-model`, `--standards-effort`, `--spec-model`, and `--spec-effort` pin one axis at a
time. Omit them and the vendor's own configuration applies. `--recover-session` re-attaches to a
review whose driver died, and exits `3` when no live review belongs here. `--help` lists every
option.

The Bridge reads no configuration file of its own, so a review resolves the same way on every
machine it is invoked from.

## Standards sources

The Standards axis is held to the documents the checkout **tracks**: `CODING_STANDARDS.md`,
`CONTRIBUTING.md`, `AGENTS.md`, and `CLAUDE.md` at the root, and every `*.md` directly under
`docs/agents/`. These are repository configuration, so git's record answers for them rather than
the disk, and one commit resolves the same list reviewed from a main worktree and from a linked
one.

`preparation.standardsCondition` states how this checkout carries them:

- `all tracked` — `docs/agents/` states a convention and every standards document found is
  tracked, so every checkout of the commit has the same list.
- `present but untracked: <paths>` — those documents lie in this checkout and git does not track
  them. They reach no other checkout, so they are not briefed. Commit them to put them back in
  the review; the `setup-matt-pocock-skills` skill is what installs `docs/agents/`, and committing
  them is its business, not the Bridge's.
- `absent` — no convention document is in this checkout at all, tracked or not, so the repository
  states no tracker convention and none may be inferred. Whatever root standards documents it
  tracks are still briefed.
- `not a git checkout; read from the disk` — the tree has no index to ask, so what lies in it is
  the list.

The first two are independent — a checkout can state no convention and still carry an untracked
`CONTRIBUTING.md` — so where both hold, both are stated, separated by `; `.

The Bridge reads these files and reports on them. It never writes a standards document and never
writes a repository's ignore rules.

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
