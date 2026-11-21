"""The roaster screen's rules that the job and dbt share (docs/design/roaster-screen.md).

For now this holds one: when a screened place is due again. The job's selection (screen.select) and dbt's
`screen_scores.next_due` both call next_due, so the two can't drift. It reads only what a screen recorded, never the
clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# Section 3.5 (the owner's D8). A transient failure says nothing about the site, so it is asked again soon, twice;
# an answer about the site goes stale slowly; a refusal is the site saying no.
RETRY_DAYS = 28
RETRY_TRIES = 2
RESCREEN_DAYS = 180
REFUSED_DAYS = 365

# The site, or its edge, refused us: robots.txt disallows the path or answered 401 or 403, or an edge challenged.
REFUSED = ("robots_disallow", "robots_401_403", "challenge")


def transient(status: str | None, refusal: str | None) -> bool:
    """A screen that got no answer about the site and may get one soon: its robots.txt didn't answer, or the site
    answered 429, 430 or a 5xx."""
    status = status or ""
    return (
        status == "robots_unavailable"
        or refusal == "http_429"
        or status in ("http_429", "http_430")
        or status.startswith("http_5")
    )


def next_due(
    screened_at: datetime, status: str | None, refusal: str | None, tier: str | None = None, tries: int = 1
) -> date:
    """The first day a place screened at `screened_at` may be screened again.

    tries: how many screens in a row, this one included, were transient: the first waits RETRY_DAYS and the
    second RESCREEN_DAYS, so a place is tried twice. tier: the screen's tier, when the caller
    has one. Every answered tier (suggested, review, dropped) shares one window today, so the tier doesn't change
    the answer; it is a parameter so a later window per tier changes this function and nothing else. A place the
    owner has flagged is never due: selection leaves it out before asking."""
    day = screened_at.date()
    if refusal in REFUSED:
        return day + timedelta(days=REFUSED_DAYS)
    if transient(status, refusal):
        return day + timedelta(days=RETRY_DAYS if tries < RETRY_TRIES else RESCREEN_DAYS)
    return day + timedelta(days=RESCREEN_DAYS)
