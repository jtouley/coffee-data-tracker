"""What the screens say about shots: numbers, the dial-in target and one-line summaries. Descriptions only;
the app doesn't suggest a grind change (PLAN, "Later: a dial-in model")."""

from __future__ import annotations

from datetime import datetime, tzinfo
from html import escape

from coffeedata.models import Shot
from coffeedata.shots import TARGET_RATIO, TARGET_TIME

_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def g(x: float | None, digits: int | None = None) -> str:
    if x is None:
        return ""
    return f"{x:.{digits}f}" if digits is not None else f"{x:g}"


def n_shots(n: int) -> str:
    return f"{n} shot{'' if n == 1 else 's'}"


def ratio(s: Shot) -> float:
    return s.yield_g / s.dose_g


def window(s: Shot) -> str:
    if s.time_s < TARGET_TIME[0]:
        return "fast"
    return "slow" if s.time_s > TARGET_TIME[1] else "in"


def _seconds(n: int) -> str:
    word = _WORDS[n] if n < 10 else str(n)
    return f"{word} second{'s' if n != 1 else ''}"


def _when(prev: Shot, now: Shot, tz: tzinfo) -> str:
    a, b = prev.brewed_at.astimezone(tz), now.brewed_at.astimezone(tz)
    if a.date() == b.date():
        return "the last shot"
    if (b.date() - a.date()).days < 7:
        return f"{a:%A}’s shot"
    return f"the {a:%b} {a.day} shot"


def summary(s: Shot, prev: Shot | None, tz: tzinfo) -> str:
    """'Inside the window, two seconds slower than Monday’s shot.' What happened, not what to change."""
    w = window(s)
    out = "Inside the window" if w == "in" else f"Ran {w} at {g(s.time_s)} s, outside the window"
    if prev:
        d = round(s.time_s - prev.time_s)
        when = _when(prev, s, tz)
        if d == 0:
            out += f", the same time as {when}"
        elif w == "in":
            out += f", {_seconds(abs(d))} {'slower' if d > 0 else 'faster'} than {when}"
        else:
            out += f"; {when} was {g(prev.time_s)} s"
    return out + "."


def sequence(shots: list[Shot]) -> str:
    """The target's marks in words, for the sentence under it."""
    parts = [f"{g(s.time_s)} s{' ' + s.taste if s.taste else ''}" for s in shots[-8:]]
    return f"This bag, in order: {', '.join(parts)}." if parts else ""


def target_svg(shots: list[Shot], height: int = 150, label: str = "") -> str:
    """The dial-in target: time across (18–38 s), ratio up (1:1.6–1:2.5), the window shaded, each shot a mark
    numbered in order and joined oldest to newest, the newest ringed."""
    left, right, top, bottom = 32, 350, 8, height - 24

    def x(t: float) -> float:
        return left + (min(max(t, 18), 38) - 18) / 20 * (right - left)

    def y(r: float) -> float:
        return top + (2.5 - min(max(r, 1.6), 2.5)) / 0.9 * (bottom - top)

    aria = label or "Shots by time and ratio against the 25 to 32 second, 1:1.8 to 1:2.3 window"
    out = [f'<svg class="target" viewBox="0 0 358 {height}" role="img" aria-label="{escape(aria)}">']
    for t in (20, 25, 30, 35):
        out.append(
            f'<line class="tick" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{bottom}"/>'
            f'<text x="{x(t):.1f}" y="{height - 6}" text-anchor="middle">{t} s</text>'
        )
    for r in (1.8, 2.0, 2.2, 2.4):
        out.append(
            f'<line class="tick" x1="{left}" x2="{right}" y1="{y(r):.1f}" y2="{y(r):.1f}"/>'
            f'<text x="{left - 6}" y="{y(r) + 4:.1f}" text-anchor="end">1:{r:.1f}</text>'
        )
    (t0, t1), (r0, r1) = TARGET_TIME, TARGET_RATIO
    out.append(
        f'<rect class="win" x="{x(t0):.1f}" y="{y(r1):.1f}" width="{x(t1) - x(t0):.1f}" '
        f'height="{y(r0) - y(r1):.1f}" rx="4"/>'
        f'<text class="wl" x="{x(t1) - 6:.1f}" y="{y(r0) - 6:.1f}" text-anchor="end">window</text>'
        f'<line class="ax" x1="{left}" x2="{right}" y1="{bottom}" y2="{bottom}"/>'
    )
    pts = [(x(s.time_s), y(ratio(s)), s) for s in shots[-8:]]
    if len(pts) > 1:
        out.append(f'<polyline class="path" points="{" ".join(f"{px:.1f},{py:.1f}" for px, py, _ in pts)}"/>')
    for i, (px, py, s) in enumerate(pts, 1):
        if s.taste == "bitter":
            out.append(f'<rect class="s-bitter" x="{px - 4.5:.1f}" y="{py - 4.5:.1f}" width="9" height="9"/>')
        else:
            out.append(f'<circle class="s-{s.taste or "none"}" cx="{px:.1f}" cy="{py:.1f}" r="4.5"/>')
        # The order number sits above-right, clear of the newest shot's ring, and flips to above-left when the
        # mark before it is close enough for the two numbers to collide.
        gap = 13 if i == len(pts) else 8
        prev = pts[i - 2] if i > 1 else None
        left = prev is not None and abs(prev[0] - px) < 18 and abs(prev[1] - py) < 18 and prev[0] >= px - 1
        if left:
            out.append(
                f'<text class="ord" x="{px - gap:.1f}" y="{py - gap:.1f}" text-anchor="end">{i}</text>'
            )
        else:
            out.append(f'<text class="ord" x="{px + gap:.1f}" y="{py - gap:.1f}">{i}</text>')
    if pts:
        px, py, _ = pts[-1]
        out.append(f'<circle class="now" cx="{px:.1f}" cy="{py:.1f}" r="10"/>')
    out.append("</svg>")
    return "".join(out)


def day(d: datetime, tz: tzinfo) -> str:
    """'Mon 7:06', for list rows."""
    d = d.astimezone(tz)
    return f"{d:%a} {d.hour}:{d:%M}"


def clock(d: datetime, tz: tzinfo) -> str:
    d = d.astimezone(tz)
    return f"{d.hour}:{d:%M}"
