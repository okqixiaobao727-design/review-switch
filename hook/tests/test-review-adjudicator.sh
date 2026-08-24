#!/usr/bin/env bash
# Black-box suite for the review adjudicator hook: PreToolUse JSON on stdin plus environment in,
# decision JSON on stdout. Nothing here reaches into the script's internals.
#
# Case inventory is the decision table the Adjudicator now holds: coordinator standdown across
# both governed targets, the plugin reviewer denied whatever it is called with, and the
# Dispatcher — the one skill left — allowed. Every case runs with no reviewer configuration in
# reach, because the Adjudicator reads none: the Dispatcher is where that file is read.
#
# Run: bash hook/tests/test-review-adjudicator.sh
# ADJUDICATOR_UNDER_TEST overrides the script under test (defaults to the sibling copy).

set -uo pipefail

adjudicator=${ADJUDICATOR_UNDER_TEST:-"$(cd "$(dirname "$0")/.." && pwd)/review-adjudicator.sh"}

test_dir=$(mktemp -d /tmp/review-switch-hook-test.XXXXXX)
cleanup() {
  case "$test_dir" in
    /tmp/review-switch-hook-test.*) rm -rf -- "$test_dir" ;;
    *) printf 'refusing to clean unexpected path: %s\n' "$test_dir" >&2 ;;
  esac
}
trap cleanup EXIT

# A home with no reviewer configuration in it, and a config path that names nothing. A
# decision that changes when these change would be a decision read from a file.
mkdir -p "$test_dir/home"

failures=0
cases=0

# run <coordinator> <skill> <args>
run() {
  local coordinator=$1 skill=$2 args=$3
  jq -n --arg skill "$skill" --arg args "$args" \
    '{hook_event_name: "PreToolUse", tool_name: "Skill", tool_input: {skill: $skill, args: $args}}' \
  | REVIEW_COORDINATOR="$coordinator" HOME="$test_dir/home" \
    CODE_REVIEWER_FILE="$test_dir/home/no-such-file" bash "$adjudicator"
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  failures=$((failures + 1))
}

# expect_allow <name> <coordinator> <skill> <args>
expect_allow() {
  local name=$1; shift
  local out status
  cases=$((cases + 1))
  out=$(run "$@")
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "$name: expected exit 0 (allow), got exit $status"
  fi
  if [ -n "$out" ]; then
    fail "$name: expected silence (allow), got: $out"
  fi
}

# expect_deny <name> <coordinator> <skill> <args> -- <substring>...
expect_deny() {
  local name=$1; shift
  local coordinator=$1 skill=$2 args=$3; shift 3
  [ "${1:-}" = "--" ] && shift
  local out decision reason
  cases=$((cases + 1))
  out=$(run "$coordinator" "$skill" "$args")
  decision=$(jq -r '.hookSpecificOutput.permissionDecision // ""' <<<"$out" 2>/dev/null)
  if [ "$decision" != "deny" ]; then
    fail "$name: expected permissionDecision deny, got: $out"
    return
  fi
  if [ "$(jq -r '.hookSpecificOutput.hookEventName // ""' <<<"$out")" != "PreToolUse" ]; then
    fail "$name: expected hookEventName PreToolUse, got: $out"
  fi
  reason=$(jq -r '.hookSpecificOutput.permissionDecisionReason // ""' <<<"$out")
  local expected
  for expected in "$@"; do
    if [[ "$reason" != *"$expected"* ]]; then
      fail "$name: reason missing '$expected', got: $reason"
    fi
  done
}

plugin=mattpocock-skills:code-review

# --- Row 1: a coordinator owns review here; every governed target stands down -----------------
for target in "$plugin" review-switch; do
  expect_deny "standdown/$target" orchestrate "$target" "" -- \
    "orchestrate" "already given to this session"
done

# Any coordinator name works: the Adjudicator reads non-emptiness only.
expect_deny "standdown/third-party" third-party-runner review-switch "" -- "third-party-runner"

# What the caller wrote in the args changes nothing about a standdown.
expect_deny "standdown/with-args" orchestrate "$plugin" "review the branch" -- "orchestrate"

# --- Row 2: a manual session; the plugin reviewer is denied whatever it carries ----------------
expect_deny "plugin/bare" "" "$plugin" "review the branch" -- "/review-switch"
expect_deny "plugin/no-args" "" "$plugin" "" -- "/review-switch"

# The retired sentinels open nothing: they are ordinary words in the args now.
expect_deny "plugin/retired-forward" "" "$plugin" "review the branch via=review-switch" -- \
  "/review-switch"
expect_deny "plugin/retired-fallback" "" "$plugin" "via=review-switch via=codex-fallback" -- \
  "/review-switch"
expect_deny "plugin/retired-router" "" "$plugin" "via=code-review-router" -- "/review-switch"

# --- Row 3: the Dispatcher is the one skill left, and it is what every deny points at ---------
expect_allow "dispatcher/with-target" "" review-switch "review the branch"
expect_allow "dispatcher/no-target" "" review-switch ""
expect_allow "dispatcher/reviewer-named" "" review-switch "review the branch --reviewer codex"

# Skills outside the family are none of the Adjudicator's business, and the plugin reviewer is
# governed under its qualified name only.
expect_allow "ungoverned/other-skill" "" orchestrate ""
expect_allow "ungoverned/bare-name" "" code-review ""
# The lane skills are gone; their names govern nothing.
expect_allow "ungoverned/retired-cc-lane" "" review-switch-cc "review the branch"
expect_allow "ungoverned/retired-codex-lane" "" review-switch-codex "review the branch"

if [ "$failures" -eq 0 ]; then
  printf 'ok: %d cases\n' "$cases"
  exit 0
fi
printf 'failed: %d of %d cases\n' "$failures" "$cases" >&2
exit 1
