"""Small text helpers."""

from __future__ import annotations

import html
import re

_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
_WS = re.compile(r"\s+")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", s, flags=re.I)
    s = html.unescape(_TAG.sub(" ", s))
    return "\n".join(_WS.sub(" ", line).strip() for line in s.splitlines() if line.strip())


def to_float(v: object) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
