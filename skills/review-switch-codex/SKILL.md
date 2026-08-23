---
name: review-switch-codex
description: Run a code review on the Codex lane — an isolated interactive Codex TUI lineage. Use when the review-switch dispatcher names this lane, or when the Codex reviewer is asked for by name.
allowed-tools: Read, Glob, Grep, AskUserQuestion, Bash(git log:*), Bash(git branch:*), Bash(python3 ~/.claude/skills/review-switch-codex/scripts/tui_review_bridge.py:*)
---

The Codex Lane is a thin coordinator. The Bridge owns review preparation: it pins the three-dot
diff, reads the spec and standards, consults the optional code graph, and writes each Axis Brief.
The Lane's only input work is the spec-reference discovery below. Hand the Bridge references,
then apply the Rounds contract to its result.

## First review

Use the caller's fixed point and axis; ask for the fixed point when it is missing, and use `both`
when the caller leaves the axis open. When the caller supplies a spec reference, pass that
reference untouched.

When the caller supplies no spec reference, locate the reference without opening the spec:

1. Identify the originating issue reference in the commit messages (`#123`, `Closes #45`, GitLab
   `!67`, etc.) using the convention in `docs/agents/issue-tracker.md`; pass the reference rather
   than fetching or reading the issue.
2. Otherwise locate a spec file under `docs/`, `specs/`, or `.scratch/` matching the branch name
   or feature.
3. If neither yields a reference, ask the user once where the spec is. Pass the reference they
   provide. If they say there is none, run only the standards axis and state under `## Spec` that
   the user confirmed no spec was available.

Make one Bridge call from the repository being reviewed, with no session handle:

```bash
python3 ~/.claude/skills/review-switch-codex/scripts/tui_review_bridge.py \
  --base '<FIXED_POINT>' --spec '<SPEC_REFERENCE>' --axis '<AXIS>'
```

For a confirmed no-spec review, omit `--spec` and pass `--axis standards`. Forward any
caller-supplied execution options accepted by the Bridge; its `--help` is their source of truth.

The Bridge prepares both axes once and, for `both`, runs Standards and Spec concurrently in two
Codex TUI panes. Treat `preparation` as its receipt. If that report explicitly names a gap or a
required action, act on it exactly as named before declaring the review complete.

## Result

Read the single JSON result through `axes`:

- `status == "completed"`: retain every axis's `reviewSessionId` and place each non-empty
  `finalMessage` under the matching heading.
- `status == "partially_completed"`: place each completed axis's `finalMessage` and each
  incomplete axis's `reason` under the matching heading. Retain every non-empty handle, then run
  an incomplete axis again as an ordinary single-axis review.
- A hard error or malformed result ends this lane's review. Report it exactly.

Use these headings for the axes returned, preserving the Bridge's report text without merging or
reranking it:

```markdown
## Standards
<axes.standards.finalMessage or reason>

## Spec
<axes.spec.finalMessage or reason>
```

After a `both` review, keep the Spec handle for the one automatic re-review the Rounds contract
allows. Keep the Standards handle too, solely so a human can wake that session by hand.

## Recovery

When a review started but its JSON result was lost, use the Bridge's recovery mode before
starting another review. Recovery re-attaches to every live axis owned by this tmux pane and
worktree; retain every recovered handle and process the result above. Exit code 3 means no live
review belongs here and licenses a new first review. A partially complete recovery follows the
same ordinary single-axis re-run rule.

## Rounds

This section is the sole authority on round capping.

Fix accepted Standards findings in one pass; Standards gets no automatic re-review. Spec
findings that required fixes earn one re-review, using that axis's exact handle and scoped to
exactly those fixes.

If that re-review leaves a Spec finding open, or a finding is reopened after it was ruled on,
end the review as a disagreement and escalate both positions to whoever requested the review.

**Completion criterion:** every returned axis is reported unchanged, every preparation gap is
handled, and every automatic follow-up stays within the Rounds cap using its own axis handle.
