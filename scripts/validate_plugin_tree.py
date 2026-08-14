#!/usr/bin/env python3
"""Validate a Review-Switch tree for private residue."""

import argparse
import pathlib
import re
import sys


LOCAL_IDENTIFIERS_FILE = ".review-switch-local-identifiers"

# These names are deliberately assembled from halves so the validator does not
# match its own policy when it scans the shipped tree.
PRIVATE_APP_NAME = "ChatGPT " + "Hands" + "-Free"
PRIVATE_SKILL_NAME = "codex" + "-implement"
BRIDGE_COMMAND = "bridge" + "ctl"
PRIVATE_BRIDGE_PATH = re.compile(
    r"(?:~|\$HOME|/(?:Users|home)/[^/\s]+)[^\n]*(?:"
    + re.escape(PRIVATE_SKILL_NAME)
    + "|"
    + re.escape(PRIVATE_APP_NAME)
    + "|"
    + re.escape(BRIDGE_COMMAND)
    + ")",
    re.IGNORECASE,
)
PRIVATE_ENV_PREFIX = "HANDS" + "_FREE_"
PRIVATE_ENV_TOKEN = re.compile(re.escape(PRIVATE_ENV_PREFIX) + r"[A-Z0-9_]+")
_CURRENCY_AMOUNT = (
    r"(?:(?:\d{1,3}(?:,\d{3})+|\d{2,})(?:\.\d+)?|\d+\.\d{2})"
)
_UNIT_AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
SPEND_FIGURE = re.compile(
    r"(?:"
    + rf"[$€£¥]\s*{_CURRENCY_AMOUNT}(?![A-Za-z0-9_]|:)"
    + rf"|[$€£¥]\s*{_UNIT_AMOUNT}\s*(?:/(?:month|mo|year)|per\s+(?:month|year)|"
    + r"(?:USD|NZD|AUD|CAD|EUR|GBP|CNY|RMB)\b)"
    + rf"|\b{_UNIT_AMOUNT}\s*(?:USD|NZD|AUD|CAD|EUR|GBP|CNY|RMB|dollars?|"
    + r"/(?:month|mo|year)|per\s+(?:month|year))\b"
    + r"|\b(?:total_cost|cost|price|spend|amount)(?:_[a-z]+)?\b[\"']?\s*[:=]\s*"
    + r"\d+(?:\.\d+)?\b"
    + r")",
    re.IGNORECASE,
)

RESIDUE_RULES = (
    (PRIVATE_BRIDGE_PATH, "{match} — private bridge path is not public"),
    (PRIVATE_ENV_TOKEN, "{match} — private environment token is not public"),
    (SPEND_FIGURE, "{match} — spend figure is not public"),
)


def local_identifiers(root):
    """Read comma- or whitespace-separated identifiers, or none when unconfigured."""
    try:
        lines = (root / LOCAL_IDENTIFIERS_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ()
    configured = " ".join(line.split("#", 1)[0] for line in lines)
    return tuple(token for token in re.split(r"[,\s]+", configured) if token)


def local_identifier_pattern(identifiers):
    """Build a whole-token matcher, including hyphens, or no matcher when absent/empty."""
    if not identifiers:
        return None
    return re.compile(
        r"(?<![A-Za-z0-9_-])(?:"
        + "|".join(re.escape(identifier) for identifier in identifiers)
        + r")(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )


def ignored_directory_names(root):
    """Read exact directory patterns from the tree's ignore policy plus VCS metadata."""
    names = {".git"}
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return names
    for line in lines:
        pattern = line.split("#", 1)[0].strip()
        if (
            pattern
            and not pattern.startswith("!")
            and pattern.endswith("/")
            and "/" not in pattern.rstrip("/")
            and not any(character in pattern for character in "*?[")
        ):
            names.add(pattern.rstrip("/"))
    return names


def shipped_text_files(root):
    """Yield readable shipped files while skipping VCS/build artifacts and policy data."""
    ignored_directories = ignored_directory_names(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in ignored_directories for part in relative.parts[:-1]):
            continue
        if relative.name == LOCAL_IDENTIFIERS_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        yield relative, text


def residue_rules(root):
    """Return fixed rules plus the local identifier rule when configured."""
    pattern = local_identifier_pattern(local_identifiers(root))
    if pattern is None:
        return RESIDUE_RULES
    return RESIDUE_RULES + ((pattern, "{match} — personal identifier is not public"),)


def validate(root):
    """Return every residue finding in the tree, in scan order."""
    problems = []
    rules = residue_rules(root)
    for relative, text in shipped_text_files(root):
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern, message in rules:
                found = pattern.search(line)
                if found:
                    problems.append(
                        f"{relative}:{number}: {message.format(match=found.group())}"
                    )
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        default=pathlib.Path(__file__).resolve().parents[1],
        type=pathlib.Path,
        help="repository tree to validate (default: the tree this script ships in)",
    )
    args = parser.parse_args(argv)
    target = args.root.resolve()
    problems = validate(target)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} problem(s) in {target}", file=sys.stderr)
        return 1
    print(f"repository OK: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
