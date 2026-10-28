"""What the screens say about shots: numbers, the dial-in target and one-line summaries. Descriptions only;
the app doesn't suggest a grind change (PLAN, "Later: a dial-in model")."""

from __future__ import annotations

from datetime import datetime, tzinfo
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit

from coffeedata.models import SAFE_ID, Shot
from coffeedata.shots import TARGET_RATIO, TARGET_TIME

# Where a gear screen may send you back to, and what its back link says. Anything else goes to Setup, so a crafted
# link can't send a save anywhere off the app.
_BACK = {"/": "Today", "/setup": "Setup", "/log/gear": "Grinder and basket"}
_GEAR_PICKS = ("grinder", "basket", "machine")


def back_to(next_: str) -> tuple[str, str]:
    """The `next` of a gear screen as (path, back label): Today, Setup or the Log shot gear picker (with the
    grinder, basket and machine it had picked); anything else is Setup."""
    u = urlsplit(next_)
    if u.scheme or u.netloc or u.path not in _BACK:
        return "/setup", "Setup"
    picks = [(k, v) for k, v in parse_qsl(u.query) if k in _GEAR_PICKS and SAFE_ID.fullmatch(v)]
    return (u.path + ("?" + urlencode(picks) if picks and u.path == "/log/gear" else ""), _BACK[u.path])


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


def target_svg(shots: list[Shot], height: int = 150, label: str = "", sequence: bool = True) -> str:
    """The dial-in target: time across (18–38 s), ratio up (1:1.6–1:2.5), the window shaded, each shot a mark
    numbered in order and joined oldest to newest, the newest ringed. With `sequence=False` (a coffee's page, many
    shots across bags) the marks are plain: neither numbered nor joined."""
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
    pts = [(x(s.time_s), y(ratio(s)), s) for s in (shots[-8:] if sequence else shots)]
    if sequence and len(pts) > 1:
        out.append(f'<polyline class="path" points="{" ".join(f"{px:.1f},{py:.1f}" for px, py, _ in pts)}"/>')
    placed: list[tuple[float, float, float, float]] = []  # label boxes so far: x0, y0, x1, y1
    for i, (px, py, s) in enumerate(pts, 1):
        if s.taste == "bitter":
            out.append(f'<rect class="s-bitter" x="{px - 4.5:.1f}" y="{py - 4.5:.1f}" width="9" height="9"/>')
        elif s.taste:
            out.append(f'<circle class="s-{s.taste}" cx="{px:.1f}" cy="{py:.1f}" r="4.5"/>')
        else:  # no taste: a diamond, so it differs from sour's ring by shape, not only colour
            out.append(f'<path class="s-none" d="M{px:.1f} {py - 5.5:.1f}l5.5 5.5-5.5 5.5-5.5-5.5z"/>')
        if not sequence:
            continue
        # The order number goes in the first corner (above-right, above-left, below-right, below-left) where it
        # clears every mark, the newest shot's ring and the numbers already placed; failing that, the clearest.
        gap = 13 if i == len(pts) else 8
        w = 7 * len(str(i))
        best = None
        for dx, dy, anchor in (
            (gap, -gap, "start"),
            (-gap, -gap, "end"),
            (gap, gap + 8, "start"),
            (-gap, gap + 8, "end"),
        ):
            tx, ty = px + dx, py + dy
            box = (tx, ty - 9, tx + w, ty + 1) if anchor == "start" else (tx - w, ty - 9, tx, ty + 1)
            if box[0] < left or box[2] > right or box[1] < top - 4 or box[3] > bottom:
                continue
            clear = min(
                [
                    _gap(box, (qx, qy, qx, qy)) - (10 if j == len(pts) else 5)
                    for j, (qx, qy, _) in enumerate(pts, 1)
                    if j != i
                ]
                + [_gap(box, other) for other in placed]
                + [99.0]
            )
            if best is None or clear > best[0] + 0.5:
                best = (clear, tx, ty, anchor, box)
            if clear >= 3:
                break
        if best is None:
            best = (0.0, px + gap, py - gap, "start", (px + gap, py - gap - 9, px + gap + w, py - gap + 1))
        _, tx, ty, anchor, box = best
        placed.append(box)
        end = ' text-anchor="end"' if anchor == "end" else ""
        out.append(f'<text class="ord" x="{tx:.1f}" y="{ty:.1f}"{end}>{i}</text>')
    if sequence and pts:
        px, py, _ = pts[-1]
        out.append(f'<circle class="now" cx="{px:.1f}" cy="{py:.1f}" r="10"/>')
    out.append("</svg>")
    return "".join(out)


def _gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """The distance between two boxes (0 when they touch or overlap)."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return float((dx * dx + dy * dy) ** 0.5)


def day(d: datetime, tz: tzinfo, now: datetime | None = None) -> str:
    """'Mon 7:06' for list rows in the last six days; 'Oct 18 7:06' before that, so two Sundays don't look alike."""
    d = d.astimezone(tz)
    today = (now or datetime.now(tz)).astimezone(tz).date()
    if 0 <= (today - d.date()).days < 6:
        return f"{d:%a} {d.hour}:{d:%M}"
    return f"{d:%b} {d.day} {d.hour}:{d:%M}"


def date(d: datetime, tz: tzinfo) -> str:
    """'Oct 21', for when something happened."""
    d = d.astimezone(tz)
    return f"{d:%b} {d.day}"


def clock(d: datetime, tz: tzinfo) -> str:
    d = d.astimezone(tz)
    return f"{d.hour}:{d:%M}"
