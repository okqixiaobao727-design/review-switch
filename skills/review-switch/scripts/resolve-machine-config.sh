#!/usr/bin/env bash
# Name what this machine configures for a review: the Lane it is set to, and the Lifecycle
# Hook options the Bridge should be called with.
#
# This is the Dispatcher's reader, and the Dispatcher is the only caller inside a Claude
# session that reads either location — a caller outside one passes both as arguments and
# reads no file at all.
#
#   ~/.claude/code-reviewer      the exact value "codex" selects the codex Lane; any other
#                                value, an empty file, or a missing file, the claude Lane.
#   ~/.claude/review-hooks/      one file per Bridge lifecycle point — child-launch,
#                                review-start, axis-end, review-end — each holding one
#                                command. A missing directory, a missing file, or a file
#                                that is blank leaves that point unset; the command is the
#                                file's first non-empty line, trimmed. Any other name in the
#                                directory is not a hook.
#
# The hook commands are printed already quoted for a shell, so the Dispatcher appends the
# line to the Bridge command as it stands and a command carrying spaces or quotes reaches
# the Bridge as its author wrote it.
#
# CODE_REVIEWER_FILE and REVIEW_HOOKS_DIR override the two paths — test seams.
#
# Tests: bash skills/review-switch/tests/test-resolve-machine-config.sh

set -uo pipefail

# The Bridge's four lifecycle points, in the order a review reaches them.
HOOK_POINTS=(child-launch review-start axis-end review-end)

reviewer_file="${CODE_REVIEWER_FILE:-$HOME/.claude/code-reviewer}"
hooks_dir="${REVIEW_HOOKS_DIR:-$HOME/.claude/review-hooks}"

if [ "$(cat "$reviewer_file" 2>/dev/null)" = "codex" ]; then
  lane=codex
else
  lane=claude
fi

# The command one point is configured with, or nothing when that point is unset.
hook_command() {
  local file="$hooks_dir/$1" line
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    if [ -n "$line" ]; then
      printf '%s' "$line"
      return 0
    fi
  done <"$file"
}

# One argument as a single shell word, whatever it contains.
quote() {
  # A single-quoted word ends, gains a literal quote, and reopens: '\''
  local escaped=${1//\'/\'\\\'\'}
  printf "'%s'" "$escaped"
}

options=()
for point in "${HOOK_POINTS[@]}"; do
  command=$(hook_command "$point")
  [ -n "$command" ] || continue
  options+=("--on-$point" "$(quote "$command")")
done

printf 'Lane configured for this machine: %s\n' "$lane"
if [ "${#options[@]}" -eq 0 ]; then
  printf 'Lifecycle hook options: none configured\n'
else
  printf 'Lifecycle hook options: %s\n' "${options[*]}"
fi
