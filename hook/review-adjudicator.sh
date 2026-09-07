#!/usr/bin/env bash
# Route supported review entries to the Dispatcher, or to the declared coordinator.
# Host event JSON + REVIEW_COORDINATOR in; decision/context JSON out.
# Tests: bash hook/tests/test-review-adjudicator.sh
set -uo pipefail

input=$(cat)
event=$(jq -r '.hook_event_name // ""' <<<"$input")
skill=$(jq -r '.tool_input.skill // ""' <<<"$input")
tool=$(jq -r '.tool_name // ""' <<<"$input")

# Cache and marketplace layouts share the same upstream skill identity.
review_file='(mattpocock-skills/([^/[:space:]]+/)*|mattpocock/)skills/engineering/code-review/SKILL[.]md'

case "$event/$tool" in
  PreToolUse/Skill)
    case "$skill" in
      mattpocock-skills:code-review|code-review|review-switch) ;;
      *) exit 0 ;;
    esac
    ;;
  PreToolUse/Read)
    jq -e --arg path "$review_file" '.tool_input.file_path // "" | test("(^|/)" + $path + "$")' \
      <<<"$input" >/dev/null || exit 0
    ;;
  PreToolUse/Bash)
    # Literal cat/sed reads seen in caller sessions; not a shell interpreter.
    jq -e --arg path "$review_file" '.tool_input.command // "" | test("(^|[;\\n&|])[ \t]*(cat|sed)[ \t]+[^;\\n]*" + $path + "([[:space:]\"\u0027;]|$)")' \
      <<<"$input" >/dev/null || exit 0
    ;;
  UserPromptSubmit/*)
    jq -e '.prompt // "" | test("(^|[[:space:]])[/$](mattpocock-skills:)?(implement|code-review)([[:space:]]|$)")' \
      <<<"$input" >/dev/null || exit 0
    ;;
  *) exit 0 ;;
esac

# The runtime environment declares ownership; skill arguments cannot change it.
coordinator="${REVIEW_COORDINATOR:-}"
if [ -n "$coordinator" ]; then
  context="Review in this workspace is owned by $coordinator. Follow the review instructions already given to this session — they name the exact command to run, and it is this work's only review."
else
  context="For the initial Code Review, follow /review-switch with the same target and arguments."
fi

if [ "$event/$tool" = "PreToolUse/Skill" ]; then
  if [ "$skill" = review-switch ] && [ -z "$coordinator" ]; then
    exit 0
  fi
  jq -n --arg reason "$context" '{hookSpecificOutput: {
    hookEventName: "PreToolUse", permissionDecision: "deny",
    permissionDecisionReason: $reason}}'
else
  if [ -z "$coordinator" ]; then
    dispatcher_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../skills/review-switch" && pwd) || exit 0
    context="$context Read $dispatcher_dir/SKILL.md. For implement, do this when implementation reaches its review step. Inspection alone does not request a review. If the Dispatcher has already selected operational-failure fallback, follow the original Matt review content directly as it instructs."
  fi
  # Context adds instructions only; it never grants tool permission.
  jq -n --arg event "$event" --arg context "$context" '{hookSpecificOutput: {
    hookEventName: $event, additionalContext: $context}}'
fi
