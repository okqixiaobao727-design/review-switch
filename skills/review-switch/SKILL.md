---
name: review-switch
description: Dispatch a review to the reviewer lane this machine is configured for. Use when a review is asked for without naming a lane, or when a review-skill invocation was refused and pointed here.
allowed-tools: Read, Write, Glob, Grep, AskUserQuestion, Bash(git log:*), Bash(git branch:*), Bash(gh api:*), Bash(bash ~/.claude/skills/review-switch/scripts/resolve-machine-config.sh:*), Bash(review-bridge:*)
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

When the caller asks to review documents — a spec, a ticket set, an issue's sub-issues, or a
`crewtask/<n>/` run — rather than a change, read `references/document-review.md` and use it to
complete the call. The Result section and everything after it apply unchanged.

## Completing the call

Use the caller's fixed point and axis; ask for the fixed point when it is missing, and use
`both` when the caller leaves the axis open. When the caller supplies a spec reference, pass
that reference untouched.

When the caller supplies no spec reference, locate the reference without opening the spec:

1. Identify the originating issue reference in the commit messages (`#123`, `Closes #45`, GitLab
   `!67`, etc.) using the convention in `docs/agents/issue-tracker.md`; pass the reference rather
   than fetching or reading the issue. Read that file by path. An ignore-aware search reports an
   ignored file as absent, so its silence is no evidence the convention does not exist. When the
   path genuinely holds nothing, pass the reference the commit messages carry and say in the
   preparation line that the repository documents no tracker convention — never infer one.
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

- Retain every non-empty `reviewSessionId`.
- A hard error or malformed result ends this review. Report it exactly.

Then do what the axis's `next` field names, and nothing else: the Bridge holds the round cap
and every result says what this lineage is permitted after it. The five actions are `done`,
`fix and stop`, `fix then one re-review`, `run again`, and `escalate`. When that action is a
Bridge call, run `nextCall.argv` exactly; the result already carries the fresh or resumed
single-axis call.
The per-axis `nextCall` of one result may be run together: two re-reviews of one result are two
lineages, and neither excludes the other. A call a lock refuses spends no round.
Where `next` is `done`, the reviewer counted no finding on that round: end the review for that
axis and report it as complete, naming `axes.<axis>.reportFile`. Nothing is in dispute, so none
of the escalation wording below belongs to it.
Follow `run again` at most once per axis per invocation. If that same axis returns `run again`
again, make no further Bridge call; report that axis as incomplete with its `reason`, and leave
the next decision to the user.
If `next` names a Bridge call but `nextCall` is `null`, this is a legacy result; report that no
safe follow-up call is available and stop rather than reconstructing one from prose.
Where `next` is `escalate`, the act is this skill's to choose, and here it is to end the review as
a disagreement and put both positions to whoever asked for it — naming
`axes.<axis>.reportFile` and `preparation.responseFile`, so the reader opens each side where it was
written. A completed re-review escalates only over a finding it retained or one a fix brought in;
`axes.<axis>.findings` says which, as `{"retained": n, "new": m}` — or `{"reported": n}` on a
first round, and `null` where the report carried no such line. A `refused` result carries no
`preparation` or Next Call; name the Response file you passed to the round that was granted.

Treat `preparation` as the Bridge's receipt. If the result explicitly names a gap or a required
action, act on it exactly as named before declaring the review complete.

### The re-review

Where `next` is `fix then one re-review`, the Bridge grants that round only against a
**Response**: one line saying what you did with each finding of the round just delivered. A
finding you decided not to fix and never said so about looks to the reviewer exactly like one you
ignored, so it is retained and the lineage escalates over a message that never arrived. A finding
not worth fixing is `declined` with its reason, so the re-review can close it: never fixed only to
make the round come back clean, and never left out.

Write one line per finding, in report order and in the shape `nextCall.responseFormat` shows, to
`nextCall.responseFile`. A `fixed` line names where in one clause, so the reviewer checks it
against the diff instead of hunting for it. Keep every line as short as the decision allows: what
you owe the reviewer is your decisions, not a second report. Then run `nextCall.argv` exactly.

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
When `preparation.specFailure` is non-null,
report its value without dropping or interpreting any detail.
When it is `null`, add no failure text: a legacy `not fetched: <reference>` still says the spec
was unavailable, but the Bridge has no stored reason for the Dispatcher to invent.

Whenever `preparation.standardsCondition` is anything other than `all tracked`, append
` · Standards: <preparation.standardsCondition>` to that line and state it verbatim. A
`present but untracked: <paths>` condition means those documents live in this checkout alone and
were left out of the review: name `setup-matt-pocock-skills` as the skill that installs
`docs/agents/`, and say that committing those files is what puts them back in every checkout's
review. Review-Switch changes no file's contents and no repository's ignore rules to fix it.

An incomplete axis has `reportFile` as `null`; its line carries its `reason` instead.

After a `both` review, keep the Spec handle for the re-review its result's `next` may name.
Keep the Standards handle too, solely so a human can wake that session by hand.

## Recovery

When a review started but its JSON result was lost, use the Bridge's recovery mode before
starting another review: run the same command with `--recover-session`. `--help` is the recovery
rule. Retain every recovered handle and process the result above.

**Completion criterion:** every returned axis has its line — summary and report path, or
`reason` — every preparation gap is handled, and every follow-up is the one that axis's `next`
named.
