#!/usr/bin/env bash
# Adjudicate every review-skill invocation: who owns this session's review, and whether the
# invoked skill is the one that may run it.
#
# PreToolUse hook, matcher Skill, installed once — globally, in ~/.claude/settings.json. The
# governed targets are the plugin reviewer (mattpocock-skills:code-review) and the Dispatcher
# (review-switch); every other skill passes untouched. The Bridge is a command rather than a
# skill, so this hook never fires on it — prompt text is what points a caller at it.
#
# Two session states, decided by REVIEW_COORDINATOR:
#   - Non-empty: a coordinator (orchestrate or any third party) has declared this workspace's
#     review routing its own in the worktree's project settings. Reviewer judgment stands down and
#     every governed target is denied, pointing the agent back at the instructions it already has.
#     The variable is runtime-injected from settings, so a model in the session cannot forge or
#     remove it — the reason the contract is an env var rather than an args token.
#   - Empty or absent: a manual session. Absence means manual by design, so version skew or a
#     forgotten declaration degrades to the conservative behaviour.
#
# In a manual session the Dispatcher runs and the plugin reviewer does not. The plugin reviewer
# is the in-session protocol this project deleted: it reviews inside the caller's own session,
# which is the one place a review may not run, so it is denied whatever it is invoked with and
# pointed at the Dispatcher. Nothing in the args opens it — the sentinels that once did retired
# with the forward they permitted, and this hook reads no configuration at all: which Lane a
# review runs on is the Dispatcher's to resolve, from a file only it reads.
#
# Tests: bash tests/test-review-adjudicator.sh (black box: stdin JSON + env in, decision out).

set -uo pipefail

PLUGIN_REVIEWER="mattpocock-skills:code-review"
DISPATCHER="review-switch"

input=$(cat)
skill=$(jq -r '.tool_input.skill // ""' <<<"$input")

case "$skill" in
  "$PLUGIN_REVIEWER"|"$DISPATCHER") ;;
  *) exit 0 ;;
esac

deny() {
  jq -n --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

coordinator="${REVIEW_COORDINATOR:-}"
if [ -n "$coordinator" ]; then
  deny "Review in this workspace is owned by $coordinator. Follow the review instructions already given to this session — they name the exact command to run, and it is this work's only review."
fi

if [ "$skill" = "$PLUGIN_REVIEWER" ]; then
  deny "That reviewer runs inside this session, and a review does not run here. Invoke \`/review-switch\` with the same target: it resolves the reviewer this machine is configured for and runs the review outside this session."
fi

# No objection. Silence is not approval — the normal permission flow still runs.
exit 0
