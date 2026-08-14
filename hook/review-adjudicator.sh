#!/usr/bin/env bash
# Adjudicate every review-skill invocation: who owns this session's review, and which lane runs it.
#
# PreToolUse hook, matcher Skill, installed once — globally, in ~/.claude/settings.json. The
# governed targets are the plugin reviewer (mattpocock-skills:code-review) and the review-switch
# family; every other skill passes untouched.
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
# In a manual session the configured reviewer decides the lane: the exact value "codex" in
# ~/.claude/code-reviewer selects the Codex lane, any other value or a missing file the Claude
# lane. CODE_REVIEWER_FILE overrides the path; it is a test seam.
#
# A bare plugin-reviewer call is what upstream skill text plants ("use /code-review"), so it is
# denied and pointed at /review-switch. The cc lane's sanctioned forward carries via=review-switch
# in its args, and its codex-lane fallback after a bridge hard error carries via=codex-fallback.
# The sentinels are readable here, so a determined model could forge them; the deny reasons
# deliberately do not mention them.
#
# Tests: bash tests/test-review-adjudicator.sh (black box: stdin JSON + env in, decision out).

set -uo pipefail

PLUGIN_REVIEWER="mattpocock-skills:code-review"

input=$(cat)
skill=$(jq -r '.tool_input.skill // ""' <<<"$input")
args=$(jq -r '.tool_input.args // ""' <<<"$input")

case "$skill" in
  "$PLUGIN_REVIEWER"|review-switch|review-switch-cc|review-switch-codex) ;;
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

reviewer_file="${CODE_REVIEWER_FILE:-$HOME/.claude/code-reviewer}"
if [ "$(cat "$reviewer_file" 2>/dev/null)" = "codex" ]; then
  configured=codex
  lane_skill=/review-switch-codex
else
  configured=cc
  lane_skill=/review-switch-cc
fi

# A sentinel is a whole argument, so match it against the args split on whitespace: a string that
# merely contains one — via=review-switch-elsewhere, notvia=codex-fallback — is not the token.
args_tokens=" $(tr -s '[:space:]' ' ' <<<"$args") "
has_token() {
  [[ "$args_tokens" == *" $1 "* ]]
}

sanctioned_forward=false
if has_token "via=review-switch"; then
  sanctioned_forward=true
fi

case "$skill" in
  "$PLUGIN_REVIEWER")
    if [ "$sanctioned_forward" = false ]; then
      deny "The reviewer configured for this machine is $configured. Invoke \`/review-switch\` with the same target; it reads that configuration and dispatches to the matching lane."
    fi
    # A forward is a Claude-lane review. Under the Codex lane it is a misroute, sanctioned only as
    # the fallback after a bridge hard error.
    if [ "$configured" = codex ] && ! has_token "via=codex-fallback"; then
      deny "The reviewer configured for this machine is codex. Invoke \`$lane_skill\` with the same target. The Claude reviewer runs here only as the fallback that lane prescribes after a bridge hard error."
    fi
    ;;
  review-switch-cc)
    if [ "$configured" = codex ]; then
      deny "The reviewer configured for this machine is codex. Invoke \`$lane_skill\` with the same target, or \`/review-switch\` to let the dispatcher pick."
    fi
    ;;
  review-switch-codex)
    if [ "$configured" != codex ]; then
      deny "The reviewer configured for this machine is $configured. Invoke \`$lane_skill\` with the same target, or \`/review-switch\` to let the dispatcher pick."
    fi
    ;;
esac

# No objection. Silence is not approval — the normal permission flow still runs.
exit 0
