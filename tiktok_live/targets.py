"""Parsing and validation for user-supplied live room names."""

import re

from .errors import SafeOperationError


_USERNAME = re.compile(r"[A-Za-z0-9._]+\Z")


def parse_targets(value: str) -> tuple[str, ...]:
    """Return normalized, unique room usernames or raise a safe error."""
    targets = []
    seen = set()

    for raw_target in re.split(r"[;；]", value):
        target = raw_target.strip()
        if not target:
            continue

        if target.startswith("@"):
            target = target[1:]

        if (not target or not _USERNAME.fullmatch(target)
                or target in {".", ".."}):
            raise SafeOperationError("invalid_targets")

        folded = target.casefold()
        if folded not in seen:
            targets.append(target)
            seen.add(folded)

    if not targets:
        raise SafeOperationError("invalid_targets")

    return tuple(targets)
