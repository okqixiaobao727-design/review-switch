#!/usr/bin/env bash
# Name the lane skill for the reviewer configured in ~/.claude/code-reviewer: the exact value
# "codex" selects the Codex lane; any other value or a missing file, the Claude lane.
# CODE_REVIEWER_FILE overrides the config path — a test seam. The adjudicator hook
# (review-switch/hook/review-adjudicator.sh) reads the same file the same way, so the dispatch
# and the enforcement always name the same lane.

set -uo pipefail

reviewer_file="${CODE_REVIEWER_FILE:-$HOME/.claude/code-reviewer}"

if [ "$(cat "$reviewer_file" 2>/dev/null)" = "codex" ]; then
  printf 'Reviewer configured for this machine: codex. Lane skill: review-switch-codex\n'
else
  printf 'Reviewer configured for this machine: cc. Lane skill: review-switch-cc\n'
fi
