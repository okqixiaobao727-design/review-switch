---
name: review-switch
description: Dispatch a code review to the reviewer lane this machine is configured for. Use when a review is asked for without naming a lane, or when a review-skill invocation was refused and pointed here.
allowed-tools: Bash(bash ~/.claude/skills/review-switch/scripts/resolve-lane.sh:*), Skill(review-switch-cc), Skill(review-switch-codex)
---

!`bash ~/.claude/skills/review-switch/scripts/resolve-lane.sh`

Invoke the lane skill named above with the caller's exact target. Ask for a target first if none
was supplied. The lane owns the review; return its report unchanged.

**Completion criterion:** the lane skill's report is shown.
