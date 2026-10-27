"""GitHub issue-form bodies: '### Label' sections, with '_No response_' for fields left empty."""

from __future__ import annotations

import re

_SECTION = re.compile(r"^###\s+(.+?)\s*\n(.*?)(?=^###\s|\Z)", re.S | re.M)


def parse(body: str) -> dict[str, str]:
    """Lower-cased label -> value. Empty fields come back as ''."""
    out = {}
    for m in _SECTION.finditer(body):
        value = m.group(2).strip()
        out[m.group(1).strip().lower()] = "" if value == "_No response_" else value
    return out


def checked(value: str) -> bool:
    """Whether a checkbox field ('- [x] …') was ticked."""
    return bool(re.search(r"-\s*\[x\]", value, re.I))
