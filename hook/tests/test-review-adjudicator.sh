#!/usr/bin/env bash
# Black-box suite for the review adjudicator hook: host event JSON on stdin plus environment in,
# decision JSON on stdout. Nothing here reaches into the script's internals.
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

expected_dispatcher="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)/skills/review-switch/SKILL.md"

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
  out=$(run "$coordinator" "$skill" "$args") || fail "$name: nonzero hook exit"
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
for target in "$plugin" code-review review-switch; do
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

# Skills outside the family are none of the Adjudicator's business.
expect_allow "ungoverned/other-skill" "" orchestrate ""
expect_deny "review/bare-name" "" code-review "" -- "/review-switch"
# The lane skills are gone; their names govern nothing.
expect_allow "ungoverned/retired-cc-lane" "" review-switch-cc "review the branch"
expect_allow "ungoverned/retired-codex-lane" "" review-switch-codex "review the branch"

# Context-only events retain the host permission flow.
expect_context() {
  local name=$1 event=$2 coordinator=$3 payload=$4
  local out status
  cases=$((cases + 1))
  out=$(REVIEW_COORDINATOR="$coordinator" bash "$adjudicator" <<<"$payload")
  status=$?
  if [ "$status" -ne 0 ] || ! jq -e --arg event "$event" '
    .hookSpecificOutput | .hookEventName == $event and
    (.additionalContext | contains("review")) and
    (has("permissionDecision") | not)' <<<"$out" >/dev/null; then
    fail "$name: expected routing context without a permission decision, got: $out"
  fi
  if [ -z "$coordinator" ]; then
    for expected in "$expected_dispatcher" "Inspection alone" "fallback"; do
      [[ "$out" == *"$expected"* ]] || fail "$name: missing $expected"
    done
  fi
  if [ -n "$coordinator" ] && [[ "$out" != *"$coordinator"* ]]; then
    fail "$name: coordinator missing"
  fi
}

expect_context prompt/implement UserPromptSubmit '' \
  '{"hook_event_name":"UserPromptSubmit","prompt":"$mattpocock-skills:implement #57"}'

expect_context read/upstream PreToolUse '' \
  '{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"/opt/plugins/mattpocock-skills/9.8/skills/engineering/code-review/SKILL.md"}}'
expect_context bash/incident PreToolUse '' \
  '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"cat /home/agent/.codex/plugins/cache/mattpocock/mattpocock-skills/1.2.3/skills/engineering/code-review/SKILL.md"}}'
expect_context bash/simulation PreToolUse '' \
  '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"sed -n \"1,240p\" /new/root/mattpocock-skills/2.0/skills/engineering/code-review/SKILL.md"}}'

expect_context read/marketplace PreToolUse '' \
  '{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"/opt/plugins/marketplaces/mattpocock/skills/engineering/code-review/SKILL.md"}}'
expect_context bash/marketplace PreToolUse '' \
  '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"cat /new/root/marketplaces/mattpocock/skills/engineering/code-review/SKILL.md"}}'

for entry in /implement '$implement' /code-review '$code-review' /mattpocock-skills:code-review; do
  payload=$(jq -n --arg prompt "$entry --base main" '{hook_event_name:"UserPromptSubmit",prompt:$prompt}')
  expect_context "prompt/$entry" UserPromptSubmit '' "$payload"
  expect_context "coordinator/$entry" UserPromptSubmit owner-runner "$payload"
done
expect_context coordinator/read PreToolUse owner-runner \
  '{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"/opt/mattpocock-skills/skills/engineering/code-review/SKILL.md"}}'

for payload in \
  '{"hook_event_name":"UserPromptSubmit","prompt":"Explain how code-review works"}' \
  '{"hook_event_name":"UserPromptSubmit","prompt":"$code-review-extra"}' \
  '{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"/opt/other/skills/engineering/code-review/SKILL.md"}}' \
  '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"git status"}}' \
  '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo /opt/mattpocock-skills/skills/engineering/code-review/SKILL.md"}}' \
  '{"hook_event_name":"PostToolUse","tool_name":"Skill","tool_input":{"skill":"code-review"}}'; do
  cases=$((cases + 1))
  out=$(bash "$adjudicator" <<<"$payload") || fail "unrelated: nonzero hook exit"
  [ -z "$out" ] || fail "unrelated: expected silence, got $out"
done

# Relocating the installation must relocate the Dispatcher pointer too.
mkdir -p "$test_dir/install with spaces/hook" "$test_dir/install with spaces/skills/review-switch"
cp "$adjudicator" "$test_dir/install with spaces/hook/review-adjudicator.sh"
adjudicator="$test_dir/install with spaces/hook/review-adjudicator.sh"
expected_dispatcher="$test_dir/install with spaces/skills/review-switch/SKILL.md"
expect_context relocated/dispatcher UserPromptSubmit '' \
  '{"hook_event_name":"UserPromptSubmit","prompt":"/code-review"}'

# A broken installation must not fabricate /SKILL.md in a successful decision.
rmdir "$test_dir/install with spaces/skills/review-switch"
cases=$((cases + 1))
out=$(bash "$adjudicator" <<<'{"hook_event_name":"UserPromptSubmit","prompt":"/code-review"}' 2>"$test_dir/missing.err")
[ "$?" -eq 0 ] && [ -z "$out" ] && [ -s "$test_dir/missing.err" ] || fail "missing Dispatcher: expected diagnostic and no routing decision"

for target in "$plugin" code-review; do
  expect_deny "missing Dispatcher/$target" '' "$target" '' -- '/review-switch'
done
expect_allow 'missing Dispatcher/entry' '' review-switch ''

if [ "$failures" -eq 0 ]; then
  printf 'ok: %d cases\n' "$cases"
  exit 0
fi
printf 'failed: %d of %d cases\n' "$failures" "$cases" >&2
exit 1
