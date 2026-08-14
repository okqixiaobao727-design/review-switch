---
name: review-switch-cc
description: Run a code review on the Claude lane — the plugin reviewer. Use when the review-switch dispatcher names this lane, or when the Claude reviewer is asked for by name.
allowed-tools: Skill(mattpocock-skills:code-review)
---

Invoke `mattpocock-skills:code-review` with the caller's exact target, appending the token
`via=review-switch` to the args — the adjudicator hook reads it as the sanctioned forward. If no
target was supplied, let the upstream skill ask for it. Return its report unchanged and stop.

The upstream skill carries its own Standards and Spec axes; this lane adds no review protocol.

**Completion criterion:** the upstream report is shown.
