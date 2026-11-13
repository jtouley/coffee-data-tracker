"""Anonymous usage counting on registry.coffee and app.registry.coffee: where the page scripts send their events, and
the referral marker on links out to roasters (docs: the analytics design, sections 4 to 6).

The scripts send to a separate collector host, never to the app, so no request carries the app's session cookie;
they send without credentials and set nothing in the browser. Nothing is sent with Global Privacy Control or Do Not
Track on. The collector itself is built elsewhere; until it is deployed, sending fails silently."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

USAGE_ORIGIN = "https://e.registry.coffee"  # the only origin either page may connect to besides its own
USAGE_URL = f"{USAGE_ORIGIN}/e"
REFERRAL = (("utm_source", "registry.coffee"), ("utm_medium", "referral"))


def with_referral(url: str | None) -> str | None:
    """A roaster's or coffee's store link with `utm_source=registry.coffee&utm_medium=referral` added, keeping its
    query and fragment. A link that already carries any `utm_*` parameter is left as it is (never overwritten), and
    anything but a plain http(s) link is returned unchanged; callers check links (site.safe_url) before this."""
    if not url or not url.lower().startswith(("https://", "http://")):
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(k.lower().startswith("utm_") for k, _ in query):
        return url
    extra, kept = urlencode(REFERRAL), parts.query.rstrip("&")
    return urlunsplit(parts._replace(query=f"{kept}&{extra}" if kept else extra))
