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

Both lanes depend on the `mattpocock-skills` plugin: the CC Lane invokes
`mattpocock-skills:code-review` directly, and the Codex Lane uses it as its hard-error fallback.

The Codex Lane additionally needs `aiohttp` (`pip install aiohttp`) installed for the Python
interpreter Claude Code runs, because its review bridge imports it; the CC Lane needs nothing
beyond the plugin. Running the test suites additionally needs `pytest`.

## Skills

- `/review-switch` selects the configured reviewer lane.
- `/review-switch-cc` runs the Claude plugin reviewer.
- `/review-switch-codex` runs the isolated Codex TUI reviewer.

Set `~/.claude/code-reviewer` to `codex` for the Codex Lane; a missing file or any other value
selects the CC Lane.
