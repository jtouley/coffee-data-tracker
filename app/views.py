"""What the screens say about shots: numbers, the dial-in target and one-line summaries. Descriptions only;
the app doesn't suggest a grind change (PLAN, "Later: a dial-in model")."""

from __future__ import annotations

from datetime import datetime, tzinfo
from html import escape
from math import ceil, floor, log10
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


def g(x: float | None, digits: int | None = None) -> str:
    if x is None:
        return ""
    return f"{x:.{digits}f}" if digits is not None else f"{x:g}"


def n_shots(n: int) -> str:
    return f"{n} shot{'' if n == 1 else 's'}"


def ratio(s: Shot) -> float:
    return s.yield_g / s.dose_g


def _when(prev: Shot, now: Shot, tz: tzinfo) -> str:
    a, b = prev.brewed_at.astimezone(tz), now.brewed_at.astimezone(tz)
    if a.date() == b.date():
        return "the last shot"
    if (b.date() - a.date()).days < 7:
        return f"{a:%A}’s shot"
    return f"the {a:%b} {a.day} shot"


def summary(s: Shot, prev: Shot | None, tz: tzinfo) -> str:
    """Describe the shot without judging it against one recipe's time or ratio."""
    out = f"1:{ratio(s):.2f} in {g(s.time_s)} s"
    if prev:
        out += f"; {_when(prev, s, tz)} was 1:{ratio(prev):.2f} in {g(prev.time_s)} s"
    return out + "."


def sequence(shots: list[Shot]) -> str:
    """The target's marks in words, for the sentence under it."""
    parts = [f"1:{ratio(s):.1f} in {g(s.time_s)} s{' ' + s.taste if s.taste else ''}" for s in shots[-8:]]
    return f"This bag, in order: {', '.join(parts)}." if parts else ""


def target_svg(shots: list[Shot], height: int = 150, label: str = "", sequence: bool = True) -> str:
    """Time across, ratio up, and the 25–32 s, 1:1.8–2.3 window shaded. The axes expand to include all shown
    shots, numbered in order and joined oldest to newest, the newest ringed. With `sequence=False` (a coffee's page,
    many shots across bags) the marks are plain: neither numbered nor joined."""
    left, right, top, bottom = 32, 350, 8, height - 24
    shown = shots[-8:] if sequence else shots
    times = [s.time_s for s in shown]
    ratios = [ratio(s) for s in shown]
    tmin = max(0.0, min(18.0, min(times, default=18.0) - 2)) if times and min(times) < 18 else 18.0
    tmax = max(38.0, max(times, default=38.0) + 2) if times and max(times) > 38 else 38.0
    rmin = max(0.0, min(1.6, min(ratios) - 0.1)) if ratios and min(ratios) < 1.6 else 1.6
    rmax = max(2.5, max(ratios) + 0.1) if ratios and max(ratios) > 2.5 else 2.5

    def x(t: float) -> float:
        return left + (t - tmin) / (tmax - tmin) * (right - left)

    def y(r: float) -> float:
        return top + (rmax - r) / (rmax - rmin) * (bottom - top)

    aria = (label + ". " if label else "Shots by time and ratio. ") + (
        "Shaded area is the window: 25 to 32 seconds and 1:1.8 to 1:2.3"
    )
    out = [f'<svg class="target" viewBox="0 0 358 {height}" role="img" aria-label="{escape(aria)}">']
    for t in (20, 25, 30, 35) if (tmin, tmax) == (18, 38) else _ticks(tmin, tmax):
        out.append(
            f'<line class="tick" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{bottom}"/>'
            f'<text x="{x(t):.1f}" y="{height - 6}" text-anchor="middle">{g(t)} s</text>'
        )
    default_ratio = (rmin, rmax) == (1.6, 2.5)
    for r in (1.8, 2.0, 2.2, 2.4) if default_ratio else _ticks(rmin, rmax):
        out.append(
            f'<line class="tick" x1="{left}" x2="{right}" y1="{y(r):.1f}" y2="{y(r):.1f}"/>'
            f'<text x="{left - 6}" y="{y(r) + 4:.1f}" text-anchor="end">1:{f"{r:.1f}" if default_ratio else g(r)}</text>'
        )
    (t0, t1), (r0, r1) = TARGET_TIME, TARGET_RATIO
    out.append(
        f'<rect class="win" x="{x(t0):.1f}" y="{y(r1):.1f}" width="{x(t1) - x(t0):.1f}" '
        f'height="{y(r0) - y(r1):.1f}" rx="4"/>'
        f'<line class="ax" x1="{left}" x2="{right}" y1="{bottom}" y2="{bottom}"/>'
    )
    pts = [(x(s.time_s), y(ratio(s)), s) for s in shown]
    label_at = len(out)  # the window's label goes under the marks, once its place is known
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
    spot = _label_spot((x(t0), y(r1), x(t1), y(r0)), (left, top, right, bottom), pts, sequence, placed)
    if spot:
        tx, ty, anchor = spot
        out.insert(
            label_at, f'<text class="wl" x="{tx:.1f}" y="{ty:.1f}" text-anchor="{anchor}">window</text>'
        )
    out.append("</svg>")
    return "".join(out)


_LABEL_W = 42  # "window" in the chart's 12 px semibold, with a little room


def label_box(tx: float, ty: float, anchor: str) -> tuple[float, float, float, float]:
    """The box the window's label covers, from its text position and anchor."""
    x0 = tx - _LABEL_W if anchor == "end" else tx
    return (x0, ty - 10, x0 + _LABEL_W, ty + 2)


def _label_spot(
    win: tuple[float, float, float, float],
    plot: tuple[float, float, float, float],
    pts: list[tuple[float, float, Shot]],
    joined: bool,
    numbers: list[tuple[float, float, float, float]],
) -> tuple[float, float, str] | None:
    """Where the window's label goes: inside the box's lower-right corner when the box is tall enough, else just
    outside it (above, below, right, left), in the first spot clear of every mark, the newest shot's ring, the path
    and the order numbers. None when no spot is clear; the label is then left out (the aria-label names it)."""
    wx0, wy0, wx1, wy1 = win
    spots = [(wx1 - 6, wy1 - 6, "end")] if wy1 - wy0 >= 24 else []
    spots += [
        (wx1, wy0 - 4, "end"),
        (wx1, wy1 + 13, "end"),
        (wx1 + 6, (wy0 + wy1) / 2 + 4, "start"),
        (wx0 - 6, (wy0 + wy1) / 2 + 4, "end"),
    ]
    segs = list(zip(pts, pts[1:], strict=False)) if joined else []
    for tx, ty, anchor in spots:
        box = label_box(tx, ty, anchor)
        if box[0] < plot[0] or box[2] > plot[2] or box[1] < plot[1] - 4 or box[3] > plot[3]:
            continue
        if any(_gap(box, (px, py, px, py)) < 7 for px, py, _ in pts):
            continue
        if joined and pts and _gap(box, (pts[-1][0], pts[-1][1]) * 2) < 12:
            continue
        if any(_gap(box, n) < 2 for n in numbers):
            continue
        if any(_seg_gap(box, a[:2], b[:2]) < 3 for a, b in segs):
            continue
        return tx, ty, anchor
    return None


def _seg_gap(box: tuple[float, float, float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """The distance between a box and the line segment a-b, sampled every pixel or so."""
    n = max(1, ceil(max(abs(b[0] - a[0]), abs(b[1] - a[1]))))
    return min(
        _gap(box, (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) * 2) for k in range(n + 1)
    )


def _ticks(low: float, high: float) -> list[float]:
    """At most five readable grid labels, with round steps, on an expanded axis."""
    raw = (high - low) / 4
    magnitude = 10 ** floor(log10(raw))
    step = next(n * magnitude for n in (1, 2, 5, 10) if n * magnitude >= raw)
    return [round(i * step, 6) for i in range(ceil(low / step), floor(high / step) + 1)]


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
