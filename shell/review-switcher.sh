#!/usr/bin/env bash
# The review switcher: the one command a person types to see or change what this machine
# is configured to review with.
#
# Source it from your shell's startup file — this repository ships the function and
# sourcing it is yours to do:
#
#   . /path/to/review-switch/shell/review-switcher.sh
#
# Grammar:
#
#   review                              print the resolved Lane, model and effort
#   review [cc|codex] [model|-] [effort|-]
#                                       select a Lane, and set or clear that Lane's
#                                       model and effort; `-` clears, and a position
#                                       left out is left as it was
#   rcc / rcodex                        switch to that Lane
#
# `cc` is the word a person types for the claude Lane; the Machine Config and the Bridge
# both spell it `claude`, and translating between the two is the whole of what this
# function knows. It knows nothing about where the configuration lives or what shape it
# has: every case forwards to the Bridge's `config` mode, so the Machine Config has
# exactly one writer and one grammar.
#
# Setting a model or an effort is proved against that Lane before it is written, so a
# model the vendor will not give you fails there, in the vendor's own words, and changes
# nothing here.
#
# REVIEW_BRIDGE overrides the command this forwards to — the test seam.
#
# Tests: bash shell/tests/test-review-switcher.sh

# Print or change this machine's review configuration.
review() {
  local bridge=${REVIEW_BRIDGE:-review-bridge}
  if [ "$#" -eq 0 ]; then
    command "$bridge" config
    return
  fi
  local lane=$1
  shift
  case "$lane" in
    cc) lane=claude ;;
  esac
  command "$bridge" config "$lane" "$@"
}

# Switch to the claude Lane, forwarding anything further as `review` takes it.
rcc() {
  review cc "$@"
}

# Switch to the codex Lane, forwarding anything further as `review` takes it.
rcodex() {
  review codex "$@"
}
