#!/usr/bin/env bash
# Black-box contract checks for the Dispatcher prose callers execute.

set -uo pipefail

skill=${SKILL_UNDER_TEST:-"$(cd "$(dirname "$0")/.." && pwd)/SKILL.md"}
failures=0

require_text() {
  local text=$1
  if ! grep -Fq -- "$text" "$skill"; then
    printf 'FAIL: missing %s\n' "$text" >&2
    failures=$((failures + 1))
  fi
}

refuse_text() {
  local text=$1
  if grep -Fq -- "$text" "$skill"; then
    printf 'FAIL: stale protocol copy remains: %s\n' "$text" >&2
    failures=$((failures + 1))
  fi
}

require_text 'nextCall.responseFormat'
require_text 'nextCall.responseFile'
require_text 'nextCall.argv'
require_text 'this is a legacy result'
require_text 'safe follow-up call is available and stop'
require_text 'Follow `run again` at most once per axis per invocation'
require_text 'report that axis as incomplete with its `reason`'
require_text 'preparation.standardsCondition'
require_text 'preparation.specFailure'
require_text 'report its value without dropping or interpreting any detail'
require_text 'When it is `null`, add no failure text'
require_text 'setup-matt-pocock-skills'
refuse_text '/tmp/review-response'
refuse_text '--resume-session'
refuse_text 'partially_completed'

if [ "$failures" -eq 0 ]; then
  printf 'ok: Dispatcher follows the result-carried Next Call\n'
  exit 0
fi
printf 'failed: %d Dispatcher contract check(s)\n' "$failures" >&2
exit 1
