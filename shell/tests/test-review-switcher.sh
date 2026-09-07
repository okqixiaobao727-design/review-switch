#!/usr/bin/env bash
# Black-box suite for the review switcher: the function is sourced, then called the way a
# person calls it, and what the Bridge was asked for is what is asserted.
#
# No other suite executes this function, so without this one the whole grammar can break
# with everything else green.
#
# Two halves, because the grammar has two halves:
#
#   forwarding — a recording stub stands in for the Bridge, and each case asserts the
#                exact argv the function handed it. This is where the positions, and the
#                cc-to-claude translation, are pinned.
#   for real   — the Bridge's own `config` mode runs, with REVIEW_SWITCH_CONFIG pointing
#                at this suite's own file. Only cases that prove no model reach here:
#                setting a model runs a health probe against a vendor, and a suite may
#                contact none.
#
# Run: bash shell/tests/test-review-switcher.sh
# SWITCHER_UNDER_TEST overrides the function file (defaults to the sibling copy).
# BRIDGE_UNDER_TEST overrides the Bridge the "for real" half runs.

set -uo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
switcher=${SWITCHER_UNDER_TEST:-"$repo_root/shell/review-switcher.sh"}
bridge=${BRIDGE_UNDER_TEST:-"$repo_root/bridge/review_bridge.py"}

test_dir=$(mktemp -d /tmp/review-switch-switcher-test.XXXXXX)
cleanup() {
  case "$test_dir" in
    /tmp/review-switch-switcher-test.*) rm -rf -- "$test_dir" ;;
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

check() {
  local name=$1 expected=$2 actual=$3
  cases=$((cases + 1))
  [ "$actual" = "$expected" ] || fail "$name: expected '$expected', got '$actual'"
}

# The function under test, in this shell.
# shellcheck source=/dev/null
. "$switcher"

# --- Forwarding: every case reaches the `config` mode, and with which words --------------
stub_log="$test_dir/forwarded"
stub="$test_dir/stub-bridge"
cat >"$stub" <<'STUB'
#!/usr/bin/env bash
# Record the argv this call was made with, one word per line, and say nothing.
printf '%s\n' "$@" >"$FORWARD_LOG"
STUB
chmod +x "$stub"

# forwarded <function call...> — the argv the stub Bridge received, as one line.
forwarded() {
  : >"$stub_log"
  REVIEW_BRIDGE="$stub" FORWARD_LOG="$stub_log" "$@" >/dev/null 2>&1
  tr '\n' ' ' <"$stub_log" | sed 's/ $//'
}

check "no-arguments/prints" "config" "$(forwarded review)"
check "three-positions" "config codex gpt-5.6-sol max" \
  "$(forwarded review codex gpt-5.6-sol max)"
check "cc-is-spelled-claude-to-the-bridge" "config claude" "$(forwarded review cc)"
check "clearing-a-model" "config codex -" "$(forwarded review codex -)"
check "clearing-both" "config claude - -" "$(forwarded review cc - -)"
check "rcc" "config claude" "$(forwarded rcc)"
check "rcodex" "config codex" "$(forwarded rcodex)"
# A word carrying spaces stays one word, so a value is never split on its way through.
check "one-word-stays-one-word" "config codex a model" \
  "$(forwarded review codex "a model")"

# --- The function holds no knowledge of the file format ---------------------------------
# Everything it could know is in the Bridge; a function that named the file, its keys or
# its shape would be a second writer's worth of knowledge in the wrong place.
cases=$((cases + 1))
if stray=$(grep -nE 'toml|REVIEW_SWITCH_CONFIG|XDG_CONFIG_HOME|\[hooks\]|\.config/' \
    "$switcher" | grep -v '^[0-9]*:#'); then
  fail "no-file-format-knowledge: the function names the file's shape: $stray"
fi

# Every call path forwards to the `config` mode: the only Bridge invocations in the file
# are `config` ones.
cases=$((cases + 1))
invocations=$(grep -c 'command "\$bridge" config' "$switcher")
bridge_uses=$(grep -c 'command "\$bridge"' "$switcher")
[ "$invocations" -eq "$bridge_uses" ] && [ "$invocations" -gt 0 ] || fail \
  "every-case-forwards: $invocations of $bridge_uses Bridge calls name the config mode"

# --- For real: the Bridge's own `config` mode, against this suite's own file -------------
config_file="$test_dir/config.toml"

# real <function call...> — the function run against the real Bridge, output printed.
real() {
  REVIEW_BRIDGE="$bridge" REVIEW_SWITCH_CONFIG="$config_file" "$@" 2>&1
}

# One line's value from the printed state.
field() {
  printf '%s' "$(grep -m1 "^$2:" <<<"$1" | sed "s/^$2: //")"
}

printf 'lane = "codex"\n\n[claude]\nmodel = "opus"\neffort = "high"\n\n[codex]\nmodel = "gpt-5.6-sol"\n' \
  >"$config_file"

printed=$(real review)
check "real/prints-the-lane" "codex (config)" "$(field "$printed" Lane)"
check "real/prints-the-model" "gpt-5.6-sol (config)" "$(field "$printed" Model)"
check "real/prints-the-effort" "none (vendor)" "$(field "$printed" Effort)"

# `review cc` switches Lane, and that Lane's model and effort stay as they were.
printed=$(real review cc)
check "real/cc-switches-lane" "claude (config)" "$(field "$printed" Lane)"
check "real/cc-keeps-model" "opus (config)" "$(field "$printed" Model)"
check "real/cc-keeps-effort" "high (config)" "$(field "$printed" Effort)"

# `review codex -` clears that Lane's model — and probes nothing to do it.
printed=$(real review codex -)
check "real/clears-the-model" "none (vendor)" "$(field "$printed" Model)"
check "real/clearing-selects-the-lane" "codex (config)" "$(field "$printed" Lane)"
cases=$((cases + 1))
grep -q 'gpt-5.6-sol' "$config_file" && fail \
  "real/clears-the-model: the cleared value is still in the file"

# The other Lane's values are untouched by any of it.
printed=$(real rcc)
check "real/rcc-switches-lane" "claude (config)" "$(field "$printed" Lane)"
check "real/other-lane-untouched" "opus (config)" "$(field "$printed" Model)"
printed=$(real rcodex)
check "real/rcodex-switches-lane" "codex (config)" "$(field "$printed" Lane)"

if [ "$failures" -eq 0 ]; then
  printf 'ok: %d cases\n' "$cases"
  exit 0
fi
printf 'failed: %d of %d cases\n' "$failures" "$cases" >&2
exit 1
