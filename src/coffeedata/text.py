"""Small text helpers."""

from __future__ import annotations

import html
import re
from typing import Any

_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
_WS = re.compile(r"\s+")
_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub_surrogates(node: Any) -> Any:
    """A decoded JSON payload with every lone surrogate (a truncated emoji escape such as "\\ud83d", which json
    accepts) replaced by U+FFFD, in keys and values at any depth. Such a string can't be encoded as UTF-8, so it
    would fail the lake write for the whole chunk; U+FFFD (rather than dropping it) keeps a visible mark of what the
    store sent. A valid pair decodes to one real character, never a surrogate, and clean text is returned as is,
    so its hashes don't change. The lake stores these decoded rows, never the response bytes, so nothing raw is
    rewritten."""
    if isinstance(node, str):
        return _SURROGATE.sub("\ufffd", node) if _SURROGATE.search(node) else node
    if isinstance(node, list):
        return [scrub_surrogates(v) for v in node]
    if isinstance(node, dict):
        return {scrub_surrogates(k): scrub_surrogates(v) for k, v in node.items()}
    return node


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
