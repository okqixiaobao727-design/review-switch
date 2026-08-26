---
name: review-switch
description: Dispatch a code review to the reviewer lane this machine is configured for. Use when a review is asked for without naming a lane, or when a review-skill invocation was refused and pointed here.
allowed-tools: Read, Write, Glob, Grep, AskUserQuestion, Bash(git log:*), Bash(git branch:*), Bash(bash ~/.claude/skills/review-switch/scripts/resolve-machine-config.sh:*), Bash(review-bridge:*)
---

!`bash ~/.claude/skills/review-switch/scripts/resolve-machine-config.sh`

You are the Dispatcher: the entry point for a review asked for from inside a Claude session.
The review itself runs outside this session, in the Bridge — `review-bridge`, which owns
preparation, both Axis Briefs, delivery to the Lane, the result contract, and the round cap.
Your work is to complete the two references the Bridge cannot know, hand it what this machine
and this caller configured, and report back what it returns.

The lines above are this machine's configuration, and this skill is the only place they are
read. Use the Lane named there unless the caller named one, and append the hook options exactly
as printed unless they read `none configured`.

## Completing the call

Use the caller's fixed point and axis; ask for the fixed point when it is missing, and use
`both` when the caller leaves the axis open. When the caller supplies a spec reference, pass
that reference untouched.

When the caller supplies no spec reference, locate the reference without opening the spec:

1. Identify the originating issue reference in the commit messages (`#123`, `Closes #45`, GitLab
   `!67`, etc.) using the convention in `docs/agents/issue-tracker.md`; pass the reference rather
   than fetching or reading the issue.
2. Otherwise locate a spec file under `docs/`, `specs/`, or `.scratch/` matching the branch name
   or feature.
3. If neither yields a reference, ask the user once where the spec is. Pass the reference they
   provide. If they say there is none, run only the standards axis; the preparation line then
   reads `not provided`.

## The call

Make one Bridge call from the repository being reviewed, with no session handle:

```bash
review-bridge --reviewer '<LANE>' \
  --base '<FIXED_POINT>' --spec '<SPEC_REFERENCE>' --axis '<AXIS>' \
  <LIFECYCLE_HOOK_OPTIONS>
```

For a confirmed no-spec review, omit `--spec` and pass `--axis standards`.

Pass on what the caller asked for, and nothing they did not:

- **Lane** — a Lane the caller named overrides the configured one. `claude` and `codex` are the
  two.
- **Axis** — `standards`, `spec`, or `both`.
- **Model and effort** — `--model` and `--effort` for the whole review, or `--standards-model`,
  `--standards-effort`, `--spec-model`, and `--spec-effort` to pin one axis at a time.
- **Anything else the Bridge accepts** — `--help` is the source of truth for its options.

An option the caller did not ask for is an option you do not pass: an omitted model or effort
means the reviewing vendor's own configuration applies, and pinning one behind the caller's back
is the one thing this skill must not do.

## Result

Read the single JSON result through `axes`:

- `status == "completed"`: retain every axis's `reviewSessionId`.
- `status == "partially_completed"`: retain every non-empty handle, then run an incomplete axis
  again as an ordinary single-axis review.
- A hard error or malformed result ends this review. Report it exactly.

Then do what the axis's `next` field names, and nothing else: the Bridge holds the round cap
and every result says what this lineage is permitted after it. Where `next` is `escalate`, the
act is this skill's to choose, and here it is to end the review as a disagreement and put both
positions to whoever asked for it — naming `axes.<axis>.reportFile` and
`preparation.responseFile`, so the reader opens each side where it was written. A `refused`
result carries no `preparation`; name the Response file you passed to the round that was granted.

Treat `preparation` as the Bridge's receipt. If the result explicitly names a gap or a required
action, act on it exactly as named before declaring the review complete.

### The re-review

Where `next` is `fix then one re-review`, the Bridge grants that round only against a
**Response**: one line saying what you did with each finding of the round just delivered. A
finding you decided not to fix and never said so about looks to the reviewer exactly like one you
ignored, so it is retained and the lineage escalates over a message that never arrived.

Write the Response before you call:

1. One line per finding, in the order the report file lists them, identified by a short quote —
   the reviewer numbers nothing, and you need not ask it to:

   ```
   N. <short quote from the finding> — fixed <where> | declined <why> | deferred <ticket>
   ```

   A `fixed` line names where in one clause, so the reviewer checks it against the diff instead of
   hunting for it. Keep every line as short as the decision allows: what you owe the reviewer is
   your decisions, not a second report.
2. Write it to a file under the system temporary directory, named after the handle so two
   reviews never collide: `/tmp/review-response-<REVIEW_SESSION_ID>.md`. Never inside the
   checkout under review — the Spec brief lists that checkout's untracked files, and a Response
   written there is a file the reviewer is asked to review.
3. Call the Bridge with the axis's handle and that file:

```bash
review-bridge --reviewer '<LANE>' \
  --base '<FIXED_POINT>' --spec '<SPEC_REFERENCE>' --axis spec \
  --resume-session '<REVIEW_SESSION_ID>' --response '<RESPONSE_FILE>' \
  <LIFECYCLE_HOOK_OPTIONS>
```

A resume without `--response`, or with an empty file, is an ordinary command-line error: no round
is spent, nothing is `refused`, and you simply call again with the file. `--response` is accepted
only with `--resume-session`; a first review and a recovery take none.

The Bridge appends the Response to the re-review's turn under a heading of its own and asks the
reviewer to close each finding or retain it, and to report anything new only where a fix
introduced it — the round is scoped to the fixes, not a second sweep of the diff.

Nothing you write binds the reviewer. A finding it retains after reading your reason is a
disagreement with a rationale on both sides, and that result's `next` is `escalate`.

### What you write

The Bridge has already written each axis's report to the file its `reportFile` names. Your output
is one preparation line, then one line per axis; the report body stays in its file.

End with a one-line summary: total findings per axis, and the worst issue _within each axis_ (if
any). Don't pick a single winner across axes — that's the reranking the separation exists to
prevent. So, for a `both` review:

```markdown
Spec source: <preparation.specSource> · Code graph: <preparation.codeGraphUsed>
Standards — <n> findings; worst: <one clause>. Report: <axes.standards.reportFile>
Spec — <n> findings; worst: <one clause>. Report: <axes.spec.reportFile>
```

The preparation line states `preparation.specSource` verbatim and `preparation.codeGraphUsed`, on
every review. `not fetched: <reference>` means the Bridge could not obtain that spec and the Lane
reviewed without it — say so, rather than passing the Spec axis off as an ordinary one.

An incomplete axis has `reportFile` as `null`; its line carries its `reason` instead.

After a `both` review, keep the Spec handle for the re-review its result's `next` may name.
Keep the Standards handle too, solely so a human can wake that session by hand.

## Recovery

When a review started but its JSON result was lost, use the Bridge's recovery mode before
starting another review. Recovery re-attaches to every live axis owned by this tmux pane and
worktree; retain every recovered handle and process the result above. Exit code 3 means no live
review belongs here and licenses a new first review. A partially complete recovery follows the
same ordinary single-axis re-run rule.

**Completion criterion:** every returned axis has its line — summary and report path, or
`reason` — every preparation gap is handled, and every follow-up is the one that axis's `next`
named.
