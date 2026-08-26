---
name: review-switch
description: Dispatch a code review to the reviewer lane this machine is configured for. Use when a review is asked for without naming a lane, or when a review-skill invocation was refused and pointed here.
allowed-tools: Read, Glob, Grep, AskUserQuestion, Bash(git log:*), Bash(git branch:*), Bash(bash ~/.claude/skills/review-switch/scripts/resolve-machine-config.sh:*), Bash(review-bridge:*)
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
   provide. If they say there is none, run only the standards axis and add, below the preparation
   line the Result section still requires, that the user confirmed no spec was available.

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

- `status == "completed"`: retain every axis's `reviewSessionId` and summarise each axis from its
  own `finalMessage`.
- `status == "partially_completed"`: summarise each completed axis, and give each incomplete axis
  its `reason` in place of a summary. Retain every non-empty handle, then run an incomplete axis
  again as an ordinary single-axis review.
- A hard error or malformed result ends this review. Report it exactly.

Then do what the axis's `next` field names, and nothing else: the Bridge holds the round cap
and every result says what this lineage is permitted after it. Where `next` is `escalate`, the
act is this skill's to choose, and here it is to end the review as a disagreement and put both
positions to whoever asked for it.

Treat `preparation` as the Bridge's receipt. If the result explicitly names a gap or a required
action, act on it exactly as named before declaring the review complete.

### What you write

Each axis's report is already rendered — once, by the Bridge, into the markdown file its
`reportFile` names, and once more into the `finalMessage` the result handed you. You add a third
rendering by reprinting either, so do neither: no report body goes into your output, and nothing
is printed and then restated. What you write is a summary and a path.

End with a one-line summary: total findings per axis, and the worst issue within each axis (if
any). Don't pick a single winner across axes.

One line per axis, carrying that axis's summary and its `reportFile` path — or, for an incomplete
axis, its `reason` in place of both. Above them, one preparation line stating
`preparation.specSource` verbatim and `preparation.codeGraphUsed`. That line is unconditional: it
appears on every review, including one the caller confirmed has no spec, where the source reads
`not provided`. So, for a `both` review:

```markdown
Spec source: <preparation.specSource> · Code graph: <preparation.codeGraphUsed>
Standards — <n> findings; worst: <one clause>. Report: <axes.standards.reportFile>
Spec — <n> findings; worst: <one clause>. Report: <axes.spec.reportFile>
```

Summarise each axis from its own report and nothing else: an axis is neither merged into its
sibling nor reranked against it, which is what keeps a clean axis from masking a failing one.
An axis that produced no report has `reportFile` as `null`, and is never a completed one, so its
`reason` is what you report in place of both.

After a `both` review, keep the Spec handle for the re-review its result's `next` may name.
Keep the Standards handle too, solely so a human can wake that session by hand.

## Recovery

When a review started but its JSON result was lost, use the Bridge's recovery mode before
starting another review. Recovery re-attaches to every live axis owned by this tmux pane and
worktree; retain every recovered handle and process the result above. Exit code 3 means no live
review belongs here and licenses a new first review. A partially complete recovery follows the
same ordinary single-axis re-run rule.

**Completion criterion:** every returned axis is summarised on its own — neither merged into
another nor reranked against it — and named its report path, every preparation gap is handled,
and every follow-up is the one that axis's `next` named.
