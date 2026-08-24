# review-switch

Review-Switch dispatches Claude Code reviews to a configured Claude plugin lane or an isolated
Codex TUI lane, with an Adjudicator hook enforcing the selected route.

## Install

```bash
git clone https://github.com/okqixiaobao727-design/review-switch.git
cd review-switch
ln -sfn "$PWD/skills/review-switch"       "$HOME/.claude/skills/review-switch"
ln -sfn "$PWD/skills/review-switch-cc"    "$HOME/.claude/skills/review-switch-cc"
ln -sfn "$PWD/skills/review-switch-codex" "$HOME/.claude/skills/review-switch-codex"
```

Register `hook/review-adjudicator.sh` as a Claude Code `PreToolUse` hook matching `Skill`.

The CC Lane depends on the `mattpocock-skills` plugin and invokes
`mattpocock-skills:code-review` directly.

The Codex Lane additionally needs `aiohttp` (`pip install aiohttp`) installed for the Python
interpreter Claude Code runs, because its review bridge imports it. `code-review-graph` is an
optional Codex Lane dependency: when its CLI and an existing graph are available, the Bridge adds
navigation pointers to each Axis Brief; the review still runs without it. The CC Lane needs
nothing beyond its plugin. Running the test suites additionally needs `pytest`.

## Codex Lane

The Codex Lane is a thin coordinator around one Bridge call. The Bridge prepares the review once
from the fixed point, spec reference, and requested axis. An `axis=both` review opens two Codex TUI
panes concurrently — Standards and Spec — then closes each pane when its turn ends and returns the
two reports separately. The Lane preserves that separation and follows the action each axis's
result names, which is where the Bridge's bounded Rounds contract reaches it.

## Skills

- `/review-switch` selects the configured reviewer lane.
- `/review-switch-cc` runs the Claude plugin reviewer.
- `/review-switch-codex` runs the isolated Codex TUI reviewer.

Set `~/.claude/code-reviewer` to `codex` for the Codex Lane; a missing file or any other value
selects the CC Lane.
