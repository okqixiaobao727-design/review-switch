---
name: review-switch-codex
description: Run a code review on the Codex lane — an isolated interactive Codex TUI lineage. Use when the review-switch dispatcher names this lane, or when the Codex reviewer is asked for by name.
allowed-tools: Bash(python3 ~/.claude/skills/review-switch-codex/scripts/tui_review_bridge.py:*), Skill(mattpocock-skills:code-review)
---

Ask for a target if none was supplied. A review cycle has one isolated lineage:

- **First review:** run the bridge from the repository being reviewed without a session handle.
- **Follow-up review of the same change:** pass the exact `reviewSessionId` returned earlier in
  this Claude conversation.
- **Different change or no known handle:** begin a first review. A handle from another task,
  tmux pane, worktree, or conversation is a different lineage.

Pass the caller's target through as `<TARGET>` and nothing else. The bridge states the Rounds
contract to the reviewer itself, so the reviewing side needs no contract appended to the target
and no skill of ours installed to follow it. The Rounds section below is this side's half of that
same contract: what to do with the findings that come back.

```bash
# First review
python3 ~/.claude/skills/review-switch-codex/scripts/tui_review_bridge.py -- '<TARGET>'

# Follow-up in the same lineage
python3 ~/.claude/skills/review-switch-codex/scripts/tui_review_bridge.py \
  --resume-session '<REVIEW_SESSION_ID>' -- '<TARGET>'

# Recover a review whose JSON result never arrived
python3 ~/.claude/skills/review-switch-codex/scripts/tui_review_bridge.py --recover-session
```

When a review was started but its JSON result was lost — the command was killed, its output never
arrived, or no `reviewSessionId` is held for a review known to have started — recover rather than
start a second one: recovery re-attaches to every live review axis this tmux pane and worktree
already own, waits out the turns in flight, and prints each per-axis result with `recovered` true. Exit
code 3 means no live review belongs here, and it is the only result that licenses a first review;
starting one while the old pane lives reviews and bills the same change twice.

`--model '<MODEL>'` and `--effort '<EFFORT>'` are both optional and independent. Pass one only
when the caller asked for that specific model or reasoning effort; omit it and Codex uses its own
configured default. Whatever a first review pins carries through every follow-up in that lineage
unless a follow-up passes a new value. Valid effort values differ per model, so pass a pair the
requested model actually supports.

Parse the bridge's single JSON result through `axes`; a single-axis result has exactly one axis
entry, and a two-axis result has `standards` and `spec`. A recovered axis additionally has
`recovered == true`, confirming that its result came from the turn already in flight:

- `status == "completed"`: retain each axis's `reviewSessionId` and return its non-empty
  `finalMessage` under that axis. The Bridge has already closed every pane; the records keep the
  threads resumable.
- `status == "partially_completed"`: return every completed axis's `finalMessage` and every
  incomplete axis's `reason`, retaining any non-empty `reviewSessionId`. Re-run a failed axis as
  an ordinary single-axis review.
- A hard error or malformed result: report it exactly, announce the Claude fallback, then invoke
  `mattpocock-skills:code-review` with the caller's exact target, appending the tokens
  `via=review-switch via=codex-fallback` to the args.

## Rounds

Classify each finding on two axes: **standards** — style, naming, convention, anything that
leaves behaviour intact — and **spec** — correctness, security, deviation from the spec or
ticket.

Fix the standards findings you accept in one pass; they are done without re-review. Spec findings
that required fixes get one re-review, scoped to exactly those fixes. Most reviews end clean
after the first pass — the re-review is a cap, not a stage to fill.

When a re-review leaves a spec finding open, or the reviewer reopens a finding already ruled on,
stop reviewing and surface the disagreement with both positions to whoever asked for the review.

**Completion criterion:** the result is shown and every Codex follow-up used the exact handle from
its own review lineage.
