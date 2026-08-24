#!/usr/bin/env bash
# Black-box suite for the Dispatcher's machine-config resolver: the two configuration
# locations in, the two lines the Dispatcher reads out. Nothing here reaches into the
# script's internals.
#
# Case inventory: the Lane every value of ~/.claude/code-reviewer resolves to, and the
# Lifecycle Hook options every state of ~/.claude/review-hooks/ renders as — configured,
# unset, blank, and a command a shell must be handed back unchanged.
#
# Run: bash skills/review-switch/tests/test-resolve-machine-config.sh
# RESOLVER_UNDER_TEST overrides the script under test (defaults to the sibling copy).

set -uo pipefail

resolver=${RESOLVER_UNDER_TEST:-"$(cd "$(dirname "$0")/.." && pwd)/scripts/resolve-machine-config.sh"}

test_dir=$(mktemp -d /tmp/review-switch-resolver-test.XXXXXX)
cleanup() {
  case "$test_dir" in
    /tmp/review-switch-resolver-test.*) rm -rf -- "$test_dir" ;;
    *) printf 'refusing to clean unexpected path: %s\n' "$test_dir" >&2 ;;
  esac
}
trap cleanup EXIT

failures=0
cases=0

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  failures=$((failures + 1))
}

# A private configuration pair for one case: a reviewer file holding $1 (or none when the
# word is `missing`) and an empty hooks directory (or none when the word is `missing`).
# Prints the case directory.
scratch() {
  local reviewer=$1 hooks=$2 dir
  dir=$(mktemp -d "$test_dir/case.XXXXXX")
  [ "$reviewer" = missing ] || printf '%s\n' "$reviewer" >"$dir/code-reviewer"
  [ "$hooks" = missing ] || mkdir -p "$dir/review-hooks"
  printf '%s' "$dir"
}

# run <case-dir>
run() {
  CODE_REVIEWER_FILE="$1/code-reviewer" REVIEW_HOOKS_DIR="$1/review-hooks" \
    bash "$resolver"
}

# The value after a labelled line's colon, with its surrounding spaces removed.
field() {
  local output=$1 label=$2 line
  line=$(grep -m1 "^$label:" <<<"$output")
  printf '%s' "${line#"$label": }"
}

# expect_lane <name> <reviewer-file-contents|missing> <expected lane>
expect_lane() {
  local name=$1 reviewer=$2 expected=$3 dir output actual
  cases=$((cases + 1))
  dir=$(scratch "$reviewer" missing)
  output=$(run "$dir")
  actual=$(field "$output" "Lane configured for this machine")
  [ "$actual" = "$expected" ] || fail "$name: expected lane '$expected', got '$actual'"
}

# expect_hooks <name> <case-dir> <expected options line>
expect_hooks() {
  local name=$1 dir=$2 expected=$3 output actual
  cases=$((cases + 1))
  output=$(run "$dir")
  actual=$(field "$output" "Lifecycle hook options")
  [ "$actual" = "$expected" ] || fail "$name: expected options '$expected', got '$actual'"
}

# --- The Lane: the exact value `codex` and nothing else ----------------------------------
expect_lane "lane/codex" codex codex
expect_lane "lane/claude" claude claude
expect_lane "lane/unknown-value" cc claude
expect_lane "lane/missing-file" missing claude
expect_lane "lane/empty-file" "" claude

# --- The hooks: a directory of one-line commands, named for the Bridge's own points -------
no_hooks=$(scratch codex missing)
expect_hooks "hooks/no-directory" "$no_hooks" "none configured"

empty_dir=$(scratch codex present)
expect_hooks "hooks/empty-directory" "$empty_dir" "none configured"

one_hook=$(scratch codex present)
printf 'record-start\n' >"$one_hook/review-hooks/review-start"
expect_hooks "hooks/one-point" "$one_hook" "--on-review-start 'record-start'"

all_hooks=$(scratch codex present)
printf 'note-child\n' >"$all_hooks/review-hooks/child-launch"
printf 'note-start\n' >"$all_hooks/review-hooks/review-start"
printf 'note-axis\n' >"$all_hooks/review-hooks/axis-end"
printf 'note-end\n' >"$all_hooks/review-hooks/review-end"
expect_hooks "hooks/every-point-in-lifecycle-order" "$all_hooks" \
  "--on-child-launch 'note-child' --on-review-start 'note-start' --on-axis-end 'note-axis' --on-review-end 'note-end'"

# A file that is empty, or holds nothing but whitespace, configures no hook.
blank_hooks=$(scratch codex present)
: >"$blank_hooks/review-hooks/child-launch"
printf '   \n\t\n' >"$blank_hooks/review-hooks/review-end"
printf 'note-axis\n' >"$blank_hooks/review-hooks/axis-end"
expect_hooks "hooks/blank-files-are-unset" "$blank_hooks" "--on-axis-end 'note-axis'"

# The command is the first non-empty line, trimmed, and later lines are not read.
multiline_hooks=$(scratch codex present)
printf '\n\n  note-start  \nnot-this-line\n' >"$multiline_hooks/review-hooks/review-start"
expect_hooks "hooks/first-non-empty-line" "$multiline_hooks" "--on-review-start 'note-start'"

# A name that is not one of the four points is not a hook.
stray_hooks=$(scratch codex present)
printf 'note-start\n' >"$stray_hooks/review-hooks/review-start"
printf 'ignore-me\n' >"$stray_hooks/review-hooks/README"
printf 'ignore-me\n' >"$stray_hooks/review-hooks/on-review-end"
expect_hooks "hooks/unknown-names-ignored" "$stray_hooks" "--on-review-start 'note-start'"

# --- The options are handed to a shell, so a command survives the round trip --------------
quoted_command="printf '%s' \"it's done\""
quoted_hooks=$(scratch codex present)
printf '%s\n' "$quoted_command" >"$quoted_hooks/review-hooks/review-end"
cases=$((cases + 1))
options=$(field "$(run "$quoted_hooks")" "Lifecycle hook options")
parsed=()
if ! eval "parsed=($options)" 2>/dev/null; then
  fail "hooks/quoting: a shell could not read the options line: $options"
elif [ "${#parsed[@]}" -ne 2 ]; then
  fail "hooks/quoting: expected 2 shell words, got ${#parsed[@]}: $options"
elif [ "${parsed[0]}" != "--on-review-end" ] || [ "${parsed[1]}" != "$quoted_command" ]; then
  fail "hooks/quoting: round trip lost the command: ${parsed[*]}"
fi

if [ "$failures" -eq 0 ]; then
  printf 'ok: %d cases\n' "$cases"
  exit 0
fi
printf 'failed: %d of %d cases\n' "$failures" "$cases" >&2
exit 1
