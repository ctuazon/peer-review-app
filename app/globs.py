"""Glob matching for repository paths (port of the Laravel app's PathMatcher).

`*` matches within one path segment, `?` one character within a segment,
`**/` any number of leading segments (including none), and a trailing `**`
the rest of the path.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable


@lru_cache(maxsize=512)
def _to_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i + 1 : i + 2] == "*":
                i += 1
                if pattern[i + 1 : i + 2] == "/":
                    i += 1
                    out.append("(?:[^/]+/)*")
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(pattern: str, path: str) -> bool:
    return bool(_to_regex(pattern).match(path))


def matches_any(patterns: Iterable[str], path: str) -> bool:
    return any(matches(p, path) for p in patterns)
