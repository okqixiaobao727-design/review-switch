#!/usr/bin/env python3

"""The on-child-launch hook: what a project gets to run when a child starts.

`[hooks.on-child-launch]` in the project's `agentcrew.toml` carries a `command`
and an `env` table, both empty until the project fills them. Empty means a child
launches exactly as it would if this file did not exist — no extra environment,
no callback — so a plugin that ships neutral stays neutral.

A configured `command` runs once per child launch, in the child's own working
directory and environment, plus the two variables naming which child launched:

    AGENTCREW_CHILD_CWD           the child's working directory
    AGENTCREW_CHILD_TMUX_TARGET   the tmux pane or window the child runs in,
                                  empty for a child with no window of its own

The hook is a courtesy to whatever the project already runs. A command that
fails, hangs, or is not installed leaves the launch itself untouched.
"""

import dataclasses
import os
import pathlib
import subprocess
import tomllib

CONFIG_FILE = "agentcrew.toml"
HOOK_SECTION = ("hooks", "on-child-launch")
CWD_VAR = "AGENTCREW_CHILD_CWD"
TMUX_TARGET_VAR = "AGENTCREW_CHILD_TMUX_TARGET"
HOOK_TIMEOUT_SECONDS = 30


@dataclasses.dataclass(frozen=True)
class LaunchHook:
    command: str = ""
    env: dict = dataclasses.field(default_factory=dict)

    def child_env(self, base=None):
        """The environment a child launches with: its own, plus the hook's."""
        environment = dict(os.environ if base is None else base)
        environment.update(self.env)
        return environment

    def run(self, cwd, tmux_target=""):
        """Run the command once for the child that just launched, if there is one."""
        if not self.command:
            return
        environment = self.child_env()
        environment[CWD_VAR] = str(cwd)
        environment[TMUX_TARGET_VAR] = tmux_target
        try:
            subprocess.run(
                self.command,
                shell=True,
                cwd=cwd,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=HOOK_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return


def find_config(start):
    """The nearest `agentcrew.toml` at or above `start`, or None.

    A child runs in a worktree under the project it belongs to, so the walk
    upwards reaches the project's own config from wherever the child sits.
    """
    for directory in [pathlib.Path(start).resolve(), *pathlib.Path(start).resolve().parents]:
        candidate = directory / CONFIG_FILE
        if candidate.is_file():
            return candidate
    return None


def load_hook(cwd):
    """The hook configured for a child launching in `cwd`, empty when there is none.

    A config that cannot be read or parsed is a config that configures no hook:
    a launch is not the place to fail over a file the launch does not need.
    """
    path = find_config(cwd)
    if path is None:
        return LaunchHook()
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return LaunchHook()
    section = config
    for key in HOOK_SECTION:
        section = section.get(key) if isinstance(section, dict) else None
    if not isinstance(section, dict):
        return LaunchHook()
    command = section.get("command")
    env = section.get("env")
    return LaunchHook(
        command=command if isinstance(command, str) else "",
        env={
            name: value
            for name, value in (env or {}).items()
            if isinstance(value, str)
        }
        if isinstance(env, dict)
        else {},
    )
