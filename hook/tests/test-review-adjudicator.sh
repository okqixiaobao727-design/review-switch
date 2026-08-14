#!/usr/bin/env bash
# Black-box suite for the review adjudicator hook: PreToolUse JSON on stdin plus environment in,
# decision JSON on stdout. Nothing here reaches into the script's internals.
#
# Case inventory is the decision table in review-switch/spec-122.md: coordinator standdown across
# all four governed targets, bare-plugin denies under both configs, matching-lane allows,
# mismatched-lane denies, and the codex-fallback exception.
#
# Run: bash review-switch/hook/tests/test-review-adjudicator.sh
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

printf 'codex\n' >"$test_dir/reviewer-codex"
printf 'cc\n' >"$test_dir/reviewer-cc"

failures=0
cases=0

# run <reviewer-config: codex|cc|missing> <coordinator> <skill> <args>
run() {
  local config=$1 coordinator=$2 skill=$3 args=$4 reviewer_file
  case "$config" in
    codex) reviewer_file="$test_dir/reviewer-codex" ;;
    cc) reviewer_file="$test_dir/reviewer-cc" ;;
    missing) reviewer_file="$test_dir/no-such-file" ;;
    *) printf 'unknown config: %s\n' "$config" >&2; exit 2 ;;
  esac
  jq -n --arg skill "$skill" --arg args "$args" \
    '{hook_event_name: "PreToolUse", tool_name: "Skill", tool_input: {skill: $skill, args: $args}}' \
  | REVIEW_COORDINATOR="$coordinator" CODE_REVIEWER_FILE="$reviewer_file" bash "$adjudicator"
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  failures=$((failures + 1))
}

# expect_allow <name> <config> <coordinator> <skill> <args>
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

# expect_deny <name> <config> <coordinator> <skill> <args> -- <substring>...
expect_deny() {
  local name=$1; shift
  local config=$1 coordinator=$2 skill=$3 args=$4; shift 4
  [ "${1:-}" = "--" ] && shift
  local out decision reason
  cases=$((cases + 1))
  out=$(run "$config" "$coordinator" "$skill" "$args")
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
for target in "$plugin" review-switch review-switch-cc review-switch-codex; do
  expect_deny "standdown/$target" codex orchestrate "$target" "" -- \
    "orchestrate" "already given to this session"
done

# The standdown holds whatever this machine is configured for.
expect_deny "standdown/cc-config" cc orchestrate "$plugin" "" -- "orchestrate"

# Any coordinator name works: review-switch reads non-emptiness only.
expect_deny "standdown/third-party" codex third-party-runner review-switch-cc "" -- \
  "third-party-runner"

# The cc lane's sanctioned forward does not survive a coordinator either.
expect_deny "standdown/sentinel-forward" cc orchestrate "$plugin" "via=review-switch" -- \
  "orchestrate"

# --- Row 2: manual session, bare plugin reviewer, both configs --------------------------------
expect_deny "bare-plugin/codex" codex "" "$plugin" "review the branch" -- \
  "codex" "/review-switch"
expect_deny "bare-plugin/cc" cc "" "$plugin" "review the branch" -- \
  "cc" "/review-switch"
# A missing config file reads as the cc lane, and a bare call is still refused.
expect_deny "bare-plugin/missing-config" missing "" "$plugin" "" -- "cc" "/review-switch"

# A sentinel is a whole argument, so a string that merely contains one is still a bare call.
expect_deny "bare-plugin/prefixed-sentinel" cc "" "$plugin" "notvia=review-switch" -- "/review-switch"
expect_deny "bare-plugin/suffixed-sentinel" cc "" "$plugin" "via=review-switch-evil" -- "/review-switch"

# The retired router's sentinel opens nothing: on either lane it reads as a bare call.
expect_deny "bare-plugin/retired-sentinel" cc "" "$plugin" "via=code-review-router" -- "/review-switch"
expect_deny "bare-plugin/retired-sentinel-fallback" codex "" "$plugin" \
  "via=code-review-router via=codex-fallback" -- "/review-switch"

# The same holds for the fallback exception: only the exact token opens the Claude lane.
expect_deny "fallback/lookalike" codex "" "$plugin" "via=review-switch via=codex-fallback-evil" -- \
  "/review-switch-codex"

# --- Row 3: manual session, lane matching the configured reviewer ------------------------------
expect_allow "lane-match/codex" codex "" review-switch-codex "review the branch"
expect_allow "lane-match/cc" cc "" review-switch-cc "review the branch"
expect_allow "lane-match/cc-forward" cc "" "$plugin" "review the branch via=review-switch"
expect_allow "lane-match/missing-config" missing "" review-switch-cc ""

# The dispatcher itself is always allowed — it is what every deny points at.
expect_allow "dispatcher/codex" codex "" review-switch "review the branch"
expect_allow "dispatcher/cc" cc "" review-switch ""

# Skills outside the family are none of the adjudicator's business, and the plugin reviewer is
# governed under its qualified name only.
expect_allow "ungoverned/other-skill" codex "" orchestrate ""
expect_allow "ungoverned/bare-name" codex "" code-review ""

# --- Row 4: manual session, lane contradicting the configured reviewer -------------------------
expect_deny "lane-mismatch/cc-under-codex" codex "" review-switch-cc "review the branch" -- \
  "/review-switch-codex"
expect_deny "lane-mismatch/codex-under-cc" cc "" review-switch-codex "review the branch" -- \
  "/review-switch-cc"
expect_deny "lane-mismatch/forward-under-codex" codex "" "$plugin" "via=review-switch" -- \
  "/review-switch-codex"

# --- Exception: the Codex lane's hard-error fallback to the Claude lane ------------------------
expect_allow "fallback/codex" codex "" "$plugin" "review the branch via=review-switch via=codex-fallback"

if [ "$failures" -eq 0 ]; then
  printf 'ok: %d cases\n' "$cases"
  exit 0
fi
printf 'failed: %d of %d cases\n' "$failures" "$cases" >&2
exit 1
