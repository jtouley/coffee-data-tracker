"""The usage counting's page side (coffeedata/beacon.py, templates/_usage.js, app.js count()): the referral marker on
links out, the one collector origin in each CSP, what the scripts may and may not do, host-only app cookies, and the
privacy policy's claims about it. The behaviour in a browser is tests/test_usage_browser.py."""

import gzip
import re
from pathlib import Path

import pytest

from coffeedata import beacon, site

from .test_invites import PW, W, accounts, app, client, owner  # noqa: F401 - fixtures

ROOT = Path(__file__).parent.parent
USAGE_JS = (ROOT / "src/coffeedata/templates/_usage.js").read_text()
APP_JS = (ROOT / "app/static/app.js").read_text()
POLICY = " ".join((ROOT / "src/coffeedata/templates/privacy.html").read_text().split())


@pytest.mark.parametrize(
    "url, out",
    [
        ("https://a.coffee/p", "https://a.coffee/p?utm_source=registry.coffee&utm_medium=referral"),
        (
            "https://a.coffee/p?v=2#buy",
            "https://a.coffee/p?v=2&utm_source=registry.coffee&utm_medium=referral#buy",
        ),
        ("https://a.coffee/p?v=2&", "https://a.coffee/p?v=2&utm_source=registry.coffee&utm_medium=referral"),
        ("http://a.coffee", "http://a.coffee?utm_source=registry.coffee&utm_medium=referral"),
        # The roaster's own utm_* is never overwritten, in any letter case.
        ("https://a.coffee/p?utm_source=shop", "https://a.coffee/p?utm_source=shop"),
        ("https://a.coffee/p?x=1&UTM_Campaign=fall", "https://a.coffee/p?x=1&UTM_Campaign=fall"),
        # Anything but a plain http(s) link is left alone.
        ("javascript:alert(1)", "javascript:alert(1)"),
        ("", ""),
        (None, None),
    ],
)
def test_links_out_carry_the_referral_marker(url, out):
    assert beacon.with_referral(url) == out


def test_the_catalog_script_adds_the_same_marker():
    # catalog.json keeps the roaster's link as it is; the page adds the marker with the same rule (index.html).
    page = (ROOT / "src/coffeedata/templates/index.html").read_text()
    assert "a.href = referral(r[C.url])" in page and "utm_source=registry.coffee&utm_medium=referral" in page
    assert "/(^|&)utm_/i" in page


def test_each_csp_allows_the_collector_and_nothing_else_new():
    html = site.with_csp('<head><meta charset="utf-8"></head><script>x</script>')
    assert "connect-src 'self' https://e.registry.coffee;" in html
    from app import web

    assert "connect-src 'self' https://e.registry.coffee" in web.CSP.split("; ")
    assert f"{beacon.USAGE_ORIGIN}/e" == beacon.USAGE_URL and urls(APP_JS) == {beacon.USAGE_URL}
    assert "script-src 'self'" in web.CSP.split("; ")  # still no inline script in the app


def urls(js: str) -> set[str]:
    return set(re.findall(r'"(https://[^"]+)"', js))


def test_the_page_script_sends_without_credentials_and_stores_nothing():
    # fetch without credentials, the beacon only where fetch is missing; nothing read from or written to storage,
    # cookies or an id; GPC and DNT checked first; no third-party code or other host.
    assert 'credentials: "omit"' in USAGE_JS and "keepalive: true" in USAGE_JS
    assert USAGE_JS.index("fetch(cfg.url") < USAGE_JS.index("navigator.sendBeacon")
    for banned in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "userAgent",
        "import(",
        "<script",
    ):
        assert banned not in USAGE_JS, banned
    assert "globalPrivacyControl === true" in USAGE_JS and "doNotTrack" in USAGE_JS
    assert urls(USAGE_JS) == set()  # the collector's address comes from beacon.USAGE_URL
    # The app's count() too.
    count = APP_JS[APP_JS.index("const USAGE_URL") : APP_JS.index("function countClick")]
    assert 'credentials: "omit"' in count and "globalPrivacyControl === true" in count and "ME" not in count


def test_the_page_script_is_small():
    # About 1.5 KB a page as served (design §8: GitHub Pages compresses), comments aside.
    code = "\n".join(line for line in USAGE_JS.splitlines() if not line.strip().startswith("//"))
    assert len(gzip.compress(code.encode())) < 1600


def test_every_site_page_counts_with_its_own_surface(tmp_path):
    env = site.environment()
    legal = {
        "links": site.links(),
        "updated": site.LEGAL_UPDATED,
        "contact": "s@example.com",
        "governing_state": "MI",
    }
    for page in ("privacy", "terms", "bot"):
        html = env.get_template(f"{page}.html").render(
            **legal, usage=site.usage(page, {"release_build_id": "b1"})
        )
        assert f'"surface": "{page}"' in html and '"release": "b1"' in html
        assert '"url": "https://e.registry.coffee/e"' in html
        # Hashed into the page's CSP like its other inline script.
        csp = re.search(r"script-src ([^;]*);", site.with_csp(html)).group(1)
        assert len(csp.split()) == 1 and csp.startswith("'sha256-")
    for page in ("index", "landing"):
        assert '{% include "_usage.js" %}' in (ROOT / f"src/coffeedata/templates/{page}.html").read_text()


def test_app_cookies_are_host_only(app, owner):  # noqa: F811
    # A cookie set with Domain= would reach e.registry.coffee; none is, by the server or the page script, so even
    # the beacon fallback carries none.
    c = client(app)
    headers = []
    for r in (
        c.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=W),
        c.get("/"),
        c.post("/api/signout", json={}, headers=W),
    ):
        headers += r.headers.get_list("set-cookie")
    assert any(h.startswith("session=") for h in headers)
    assert not any("domain=" in h.lower() for h in headers)
    for f in (ROOT / "app").glob("*.py"):
        for call in re.findall(r"set_cookie\((.*?)\)\n", f.read_text(), re.S):
            assert "domain" not in call, f
    for line in re.findall(r"document\.cookie = .*", APP_JS):
        assert "domain" not in line.lower()


def test_app_shop_link_is_counted_and_marked():
    tpl = (ROOT / "app/templates/coffee.html").read_text()
    assert (
        'href="{{ raw.url | referral }}"' in tpl and "data-out" in tpl and 'data-surface="app_coffee"' in tpl
    )


def test_the_privacy_policy_says_what_is_counted():
    for claim in (
        "We count page visits, searches, filters and clicks to roasters, on the site and in the app, without cookies, "
        "without storing anything in your browser, without an ID, and never tied to your account.",
        "We never store your full IP address: we shorten it straight away (for example 203.0.113.x)",
        "The city is often your internet provider's nearby city rather than where you are.",
        "small towns are never named",
        "Links to roasters carry <code>utm_source=registry.coffee</code>.",
        "With Global Privacy Control or Do Not Track, nothing is counted.",
        'IP location data by <a href="https://db-ip.com">DB-IP</a>',
    ):
        assert claim in POLICY, claim
    assert "run analytics" not in POLICY and "doesn't collect anything about you" not in POLICY
    # Each claim holds in the code: the marker, GPC/DNT, no storage (above), and the IP masking is the collector's.
    assert beacon.REFERRAL[0] == ("utm_source", "registry.coffee")
