#!/usr/bin/env python3
"""Fixtures every suite that exercises the on-child-launch hook shares."""

import pathlib


def write_hook_config(root, command="", env=None):
    """A project config whose hook runs `command`, in TOML literal strings.

    Literal strings pass the command through as written: a shell command is full
    of the quotes and backslashes a basic string would read as escapes.
    """
    lines = ["[hooks.on-child-launch]", f"command = '{command}'"]
    if env:
        lines.append("[hooks.on-child-launch.env]")
        lines.extend(f"{name} = '{value}'" for name, value in env.items())
    (pathlib.Path(root) / "agentcrew.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def marker_lines(path):
    """What a hook command appended to its marker file, one launch per line."""
    path = pathlib.Path(path)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()
