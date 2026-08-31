#!/usr/bin/env bash
# Black-box contract checks for the Dispatcher prose callers execute.

set -uo pipefail

skill=${SKILL_UNDER_TEST:-"$(cd "$(dirname "$0")/.." && pwd)/SKILL.md"}
reference=${REFERENCE_UNDER_TEST:-"$(cd "$(dirname "$0")/.." && pwd)/references/document-review.md"}
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

require_reference_text() {
  local text=$1
  if ! grep -Fq -- "$text" "$reference"; then
    printf 'FAIL: Document Review reference missing %s\n' "$text" >&2
    failures=$((failures + 1))
  fi
}

require_text 'nextCall.responseFormat'
require_text 'nextCall.responseFile'
require_text 'nextCall.argv'
require_text 'this is a legacy result'
require_text 'safe follow-up call is available and stop'
require_text 'Follow `run again` at most once per axis per invocation'
require_text 'The per-axis `nextCall` of one result may be run together'
require_text 'A call a lock refuses spends no round.'
require_text 'report that axis as incomplete with its `reason`'
require_text 'preparation.standardsCondition'
require_text 'preparation.specFailure'
require_text 'report its value without dropping or interpreting any detail'
require_text 'When it is `null`, add no failure text'
require_text 'setup-matt-pocock-skills'
require_text 'Bash(gh api:*)'
require_text 'When the caller asks to review documents'
require_text 'read `references/document-review.md`'
require_text 'The Result section and everything after it apply unchanged.'
refuse_text '/tmp/review-response'
refuse_text '--resume-session'
refuse_text 'partially_completed'
refuse_text 'Bash(gh:*)'

if [ ! -f "$reference" ]; then
  printf 'FAIL: missing Document Review reference: %s\n' "$reference" >&2
  failures=$((failures + 1))
else
  require_reference_text 'repos/{owner}/{repo}/issues/<n>/sub_issues'
  require_reference_text 'select(.state == "open")'
  require_reference_text 'docs/agents/issue-tracker.md'
  require_reference_text "--document '#<number>'"
  require_reference_text 'Parent: <preparation.parentSource> · Documents: <n> · Standards: <preparation.standardsCondition>'
  require_reference_text 'codebase-design` skill could not be loaded'
fi

if [ "$failures" -eq 0 ]; then
  printf 'ok: Dispatcher follows the result-carried Next Call\n'
  exit 0
fi
printf 'failed: %d Dispatcher contract check(s)\n' "$failures" >&2
exit 1
