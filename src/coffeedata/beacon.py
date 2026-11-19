"""Anonymous usage counting on registry.coffee and app.registry.coffee: where the page scripts send their events, and
the referral marker on links out to roasters (docs: the analytics design, sections 4 to 6).

The scripts send to the usage collector's own Cloud Run address (config/usage.toml, collector_url), never to the
app, so no request carries the app's session cookie; they send without credentials and set nothing in the browser.
Nothing is sent with Global Privacy Control or Do Not Track on. While collector_url is empty (until the service
exists) the pages send nothing and their CSPs name no other origin."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# The checked-in address (the collector's PR owns the file; the app image carries config/).
CONFIG = Path(__file__).resolve().parents[2] / "config" / "usage.toml"
URL = re.compile(r"https://[a-z0-9-]+(\.[a-z0-9-]+)+")  # an origin: https, a host, no path or port
REFERRAL = (("utm_source", "registry.coffee"), ("utm_medium", "referral"))


def collector_url(path: Path = CONFIG) -> str | None:
    """The collector's origin from config/usage.toml, or None while it is empty or the file isn't there. Anything
    but a plain https origin raises: it goes into a CSP and a page script."""
    try:
        url = str(tomllib.loads(path.read_text()).get("collector_url") or "").strip().rstrip("/")
    except FileNotFoundError:
        return None
    if url and not URL.fullmatch(url):
        raise ValueError(f"{path}: collector_url must be an https origin, not {url!r}")
    return url or None


def events_url(collector: str | None) -> str | None:
    """Where the pages POST their events: <collector_url>/e, or None (send nothing)."""
    return f"{collector}/e" if collector else None


def with_referral(url: str | None) -> str | None:
    """A roaster's or coffee's store link with `utm_source=registry.coffee&utm_medium=referral` added, keeping its
    query and fragment. A link that already carries any `utm_*` parameter is left as it is (never overwritten), and
    anything but a plain http(s) link, or one urllib can't parse, is returned unchanged; callers check links
    (site.safe_url) before this."""
    if not url or not url.lower().startswith(("https://", "http://")):
        return url
    # A malformed link from a feed ("https://[shop.example/x") is left untagged, never an error.
    try:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return url
    if any(k.lower().startswith("utm_") for k, _ in query):
        return url
    extra, kept = urlencode(REFERRAL), parts.query.rstrip("&")
    return urlunsplit(parts._replace(query=f"{kept}&{extra}" if kept else extra))
