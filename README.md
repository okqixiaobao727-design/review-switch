# review-switch

Review-Switch runs one code review protocol and delivers it to the reviewing vendor you name:
`claude` or `codex`. Both Lanes review outside the caller session. If the Bridge cannot complete a Code Review,
the Dispatcher discloses the failure and falls back to the caller running the original Matt review.

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

### Caller hooks (Claude and Codex)

The Adjudicator needs Bash and `jq`. Merge these registrations into the existing `hooks`
object in `~/.claude/settings.json` for Claude and `~/.codex/hooks.json` for Codex. Replace
`<checkout>` with the absolute Review-Switch checkout path; retain unrelated hooks. When
upgrading the previous Claude installation, replace its Review-Switch `Skill` registration
with the expanded matcher below so the same hook runs once per event.

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Skill|Bash|Read",
      "hooks": [{"type": "command", "command": "bash \"<checkout>/hook/review-adjudicator.sh\"", "timeout": 5}]
    }],
    "UserPromptSubmit": [{
      "hooks": [{"type": "command", "command": "bash \"<checkout>/hook/review-adjudicator.sh\"", "timeout": 5}]
    }]
  }
}
```

Start a fresh trusted session after changing registration. In Codex, open `/hooks`, review
and trust the new/changed definitions; untrusted hooks are skipped. Codex exposes shell and
`exec_command` calls to this matcher as `Bash`. In Claude, inspect `/hooks` and accept the
normal settings/trust review. See the [Codex hook reference](https://learn.chatgpt.com/docs/hooks)
and [Claude hook reference](https://code.claude.com/docs/en/hooks) for host loading rules.
Verify hook delivery in a fresh session; a configuration entry alone is not proof it ran.

Both qualified Matt and bare `code-review` Skill calls redirect to the Dispatcher. Explicit
`/implement`, `$implement`, `/code-review`, and `$code-review` prompts (including qualified
names) supply routing context for the review step. Literal `cat`/`sed` and `Read` loads of
Matt's review skill supply the same context, with its Dispatcher path resolved from this
checkout. This also lets Codex load the Dispatcher without a Skill tool or another skill
installation. Reading for inspection does not start review. A declared `REVIEW_COORDINATOR`
keeps ownership. These are targeted entry instructions, not exhaustive anti-bypass controls.

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
`not fetched: <reference>`, `preparation.specFile` is `null`, and `preparation.specFailure`
contains the exact human-readable failure detail given to the axis. The axis reports that the spec
was unreachable rather than inferring requirements from the diff. `specFailure` is always present
on a preparation receipt and is `null` after a successful fetch, for a usable local spec, when no
spec was provided, and when a recovered legacy receipt never stored a reason.

It prints one JSON object: a `preparation` receipt, and one entry per axis under `axes` carrying
that axis's `status`, `finalMessage`, `reviewSessionId`, `reportFile` — a markdown file holding
that axis's report, or `null` where it produced none — and `next`, the one action you are
permitted after that result: `done`, `fix and stop`, `fix then one re-review`, `run again`, or
`escalate`. Every axis also carries `findings`, the counts the reviewer ended its report with —
`{"reported": n}` on a first round, `{"retained": n, "new": m}` on a re-review, or `null` where the
report carried no such line — and `nextCall`: the exact Bridge argv for a permitted re-review or
fresh single-axis run, including the Response file and line shape for a re-review, or `null` when
no Bridge call is permitted. `--model` and `--effort` pin the whole review;
`--standards-model`, `--standards-effort`, `--spec-model`, and `--spec-effort` pin one axis at a
time. Omit them and the vendor's own configuration applies. `--recover-session` re-attaches to a
review whose driver died, and exits `3` when no live review belongs here. `--help` lists every
option.

Where you name none of them, this machine's own configuration answers, and where it is silent
too the reviewing vendor's is what applies. See [Configuring this machine](#configuring-this-machine).

## Document Review

A Document Review holds documents to their Parent and to the checkout, using `requirements` and
`design` as separate axes. Name each document once; `--parent` is optional:

```bash
review-bridge --reviewer codex --parent '#43' --document '#48' --axis both
```

Its `preparation` receipt carries `parentSource`, `parentFile`, `parentFailure`, `documents` (one
`source` and `file` pair per document), `standardsFiles`, `standardsCondition`, `codeGraphUsed`
(`false`), and `responseFile`. An unfetched Parent is recorded and the review continues; an
unfetched document stops preparation before a Lane opens. The result keeps the same per-axis
report, Next Call, Response, rounds, and recovery contract described above.

## Standards sources

The Standards axis is held to the documents the checkout **tracks**: `CODING_STANDARDS.md`,
`CONTRIBUTING.md`, `AGENTS.md`, and `CLAUDE.md` at the root, and every `*.md` directly under
`docs/agents/`. These are repository configuration, so git's record answers for them rather than
the disk, and one commit resolves the same list reviewed from a main worktree and from a linked
one.

A repository that keeps its standards out of its published tree declares them by naming them in
a `.gitignore` it **tracks** — at the root or in any subdirectory. That rule file is committed
even where the documents are not, so every checkout of the commit reads the same declaration,
and a document it ignores is briefed exactly as a tracked one is. `.git/info/exclude`, a global
excludes file (`core.excludesFile`) and an untracked `.gitignore` are one machine's private
state and declare nothing. A directory pattern declares its directory written either way
(`docs/agents` or `docs/agents/`), and a linked worktree that reaches the directory through a
symlink resolves the same rule the main checkout does.

`preparation.standardsCondition` states how this checkout carries them:

- `all tracked` — `docs/agents/` states a convention and every standards document found is
  tracked, or declared local by a tracked `.gitignore`, so every checkout of the commit has the
  same list.
- `present but untracked: <paths>` — those documents lie in this checkout, git does not track
  them, and no tracked `.gitignore` declares them. They reach no other checkout, so they are not
  briefed. Commit them to put them back in the review; the `setup-matt-pocock-skills` skill is
  what installs `docs/agents/`, and committing them is its business, not the Bridge's.
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

A round the reviewer counted nothing on ends the lineage instead: the Bridge reads the counts off
the last line of the report, reports them as `findings`, and names `done` as the next action, with
no `nextCall`. A completed re-review is `escalate` only where a finding was retained or a fix
brought a new one in. No extra round is granted either way.

Re-reviews of different axes may run concurrently: each axis is its own lineage, so the two
per-axis `nextCall`s one result hands back can be run together from one caller.

## Lifecycle Hooks

A caller that wants a review observed hands in the commands to run: `--on-child-launch`,
`--on-review-start`, `--on-axis-end`, and `--on-review-end`, each one command string. Each runs
once in the reviewed working directory, with that point's facts in its environment as `REVIEW_*`
variables. Pass none and nothing extra runs; a command that fails, hangs, or is missing leaves
the review's result untouched.

## Configuring this machine

`review-bridge config` is the one writer of this machine's configuration — its Lane, that
Lane's model and effort, and its Lifecycle Hook commands all live in one file. With no
arguments it prints where things stand; with positions it sets them:

```bash
review-bridge config                          # print the Lane, model and effort in force
review-bridge config codex gpt-5.6-sol max    # select a Lane, and pin its model and effort
review-bridge config codex -                  # clear that Lane's model
review-bridge config claude                   # switch Lane; that Lane's own values stand
```

A position left out is left as it was, and `-` clears a value. Model and effort are kept
per Lane, so switching Lane carries switching to that Lane's own values.

A model or an effort is proved before it is written: the Bridge runs its health probe on
that Lane with the values the file is about to carry, and writes only once the Lane
answers. A Lane that refuses prints the vendor's own error, changes nothing, and exits
non-zero. There is no flag to skip it, and this repository keeps no list of model IDs —
which models you may use is the vendor's answer, not one that could go stale here.
Selecting only a Lane, or clearing a value, proves nothing and needs no network.

The switcher is the short way to type all of that. It is a shell function this repository
ships; sourcing it is yours to do:

```bash
. "$PWD/shell/review-switcher.sh"    # add this line to your shell's startup file
```

`review` prints the configuration; `review [cc|codex] [model|-] [effort|-]` sets it; `rcc`
and `rcodex` switch Lane. `cc` is the word you type for the claude Lane. Every case
forwards to `review-bridge config`, so the file has exactly one writer.

## Asking from inside a Claude or Codex session

`/review-switch` is the Dispatcher, and the only skill this project installs. It completes the
fixed point and the spec reference, and calls `review-bridge` — the same command a terminal runs.

It reads no configuration of its own: it passes on what you asked for and nothing else, so the
Lane, its model and effort, and the Lifecycle Hook commands all come from the Machine Config the
`review` switcher writes, exactly as they do for a terminal call. A Lane you name when you ask is
an argument like any other, and beats the file.

Every review it reports back names what that review ran as, read from the Bridge's own result:
the Lane on the preparation line, the model and effort on each axis's line, and beside each one
whether you pinned it, this machine did, or the vendor answered.

When a Code Review cannot complete operationally, the Dispatcher discloses the Bridge failure
and falls back to the current agent executing the unchanged original Matt review skill from
its installed catalog. Completed Bridge findings remain visible alongside that fallback.
Implementation continues; if the upstream skill is unavailable too, the caller reports that
review could not run. Completed findings and disagreements keep the existing round rules.
This fallback does not change Document Review or coordinator-owned policy.

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
