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


COLLECTOR = "https://registry-coffee-usage-123456789012.us-central1.run.app"


def test_each_csp_allows_the_collector_only_when_there_is_one():
    from app import web

    page = '<head><meta charset="utf-8"></head><script>x</script>'
    assert "connect-src 'self';" in site.with_csp(page)
    assert f"connect-src 'self' {COLLECTOR};" in site.with_csp(page, COLLECTOR)
    assert "connect-src 'self'" in web.csp().split("; ") and web.csp() == web.CSP
    assert f"connect-src 'self' {COLLECTOR}" in web.csp(COLLECTOR).split("; ")
    assert "script-src 'self'" in web.csp(COLLECTOR).split("; ")  # still no inline script in the app
    # No address is written into either script: it comes from config/usage.toml.
    assert urls(APP_JS) == set() and urls(USAGE_JS) == set()
    assert "registry.coffee/e" not in APP_JS + USAGE_JS


def test_the_collector_address_comes_from_config(tmp_path):
    cfg = tmp_path / "usage.toml"
    assert beacon.collector_url(tmp_path / "missing.toml") is None
    cfg.write_text('collector_url = ""\n')
    assert beacon.collector_url(cfg) is None and beacon.events_url(None) is None
    assert (
        site.usage("catalog", None, beacon.collector_url(cfg))["url"] is None
    )  # the script then sends nothing
    cfg.write_text(f'collector_url = "{COLLECTOR}/"\n')
    assert beacon.collector_url(cfg) == COLLECTOR and beacon.events_url(COLLECTOR) == f"{COLLECTOR}/e"
    for bad in ("http://x.run.app", "https://x.run.app/e", "https://x.run.app; script-src *", "javascript:x"):
        cfg.write_text(f'collector_url = "{bad}"\n')
        with pytest.raises(ValueError):
            beacon.collector_url(cfg)
    assert "!cfg.url ||" in USAGE_JS and "if (!USAGE_URL ||" in APP_JS


def test_the_app_names_its_collector_only_when_configured(lake, accounts):  # noqa: F811
    from fastapi.testclient import TestClient

    from app import web
    from app.auth import Sessions
    from app.main import create_app

    for collector in (COLLECTOR, ""):
        r = TestClient(create_app(lake, Sessions(accounts), usage_collector=collector)).get("/signin")
        assert r.headers["content-security-policy"] == web.csp(collector or None)
        if collector:
            assert f'<meta name="usage" content="{COLLECTOR}/e">' in r.text
        else:
            assert 'name="usage"' not in r.text


def urls(js: str) -> set[str]:
    return set(re.findall(r'"(https://[^"]+)"', js))


def test_the_page_script_sends_without_credentials_and_stores_nothing():
    # fetch without credentials, the beacon only where fetch is missing; nothing read from or written to storage,
    # cookies or an id; GPC and DNT checked first; no third-party code or other host.
    assert 'credentials: "omit"' in USAGE_JS and "keepalive: true" in USAGE_JS
    assert "sendBeacon(" not in USAGE_JS and "sendBeacon(" not in APP_JS  # it sends cookies: never used
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
            **legal, usage=site.usage(page, {"release_build_id": "b1"}, COLLECTOR)
        )
        assert f'"surface": "{page}"' in html and '"release": "b1"' in html
        assert f'"url": "{COLLECTOR}/e"' in html
        # Hashed into the page's CSP like its other inline script.
        csp = re.search(r"script-src ([^;]*);", site.with_csp(html, COLLECTOR)).group(1)
        assert len(csp.split()) == 1 and csp.startswith("'sha256-")
    for page in ("index", "landing"):
        assert '{% include "_usage.js" %}' in (ROOT / f"src/coffeedata/templates/{page}.html").read_text()


def test_app_cookies_are_host_only(app, owner):  # noqa: F811
    # A cookie set with Domain= could reach another host; none is, by the server or the page script, so even
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
    """Every field the scripts send or the collector keeps is named, search text as typed and kept for good, and
    what can't be deleted is said plainly."""
    for claim in (
        "We count page visits, searches, filters and clicks to roasters, on the site and in the app.",
        "without cookies, without storing anything in your browser, without an ID, and never tied to your account.",
        "What you searched for, exactly as you typed it.",  # query
        "The filters and sort order you chose, and how many results there were.",  # filters, results_n
        "Which roaster or coffee link you clicked, and its position in the results.",  # roaster_id, coffee_id, rank
        "Which page you were on, whether you were on a phone or a computer, and which weekly build of the catalog it "
        "showed.",  # surface, device, release
        "The site or campaign that sent you: only the other site's name, never the page.",  # referrer host, utm_*
        "The hour we received it, not the minute.",  # received_at
        "We never store your full IP address: we shorten it straight away.",  # ip_prefix
        "The city is often your internet provider's nearby city rather than where you are.",
        "We check your browser type only to mark visits from bots, and don't keep it.",  # bot
        "Our reports show only weekly totals.",
        "small towns are never named",
        "Links to roasters carry <code>utm_source=registry.coffee</code> and <code>utm_medium=referral</code>",
        "With Global Privacy Control or Do Not Track on, nothing is counted.",
        "we keep them permanently, searches and shortened IP addresses included",
        "Nothing links them to you, so we can't find or delete yours on request.",
        "To see which searches, filters and roasters people use",  # the purpose
        'IP location data by <a href="https://db-ip.com">DB-IP</a>',
    ):
        assert claim in POLICY, claim
    assert "run analytics" not in POLICY and "doesn't collect anything about you" not in POLICY
    assert "We look only at weekly totals" not in POLICY  # the owner can read the rows; reports show totals
    assert beacon.REFERRAL == (("utm_source", "registry.coffee"), ("utm_medium", "referral"))


def test_the_policy_date_moves_with_the_policy():
    # The usage text is new since #134's November 8: members are asked to accept it once (Accounts.needs_agree).
    assert site.LEGAL_UPDATED != "November 8, 2026"
    assert "SET AT MERGE (#169)" in (ROOT / "src/coffeedata/site.py").read_text()


def test_a_malformed_store_link_is_left_untagged_and_the_page_still_builds():
    bad = "https://[shop.example/x"
    assert site.safe_url(bad) == bad and beacon.with_referral(bad) == bad
    html = site.environment().from_string("{{ u | referral }}").render(u=bad)
    assert html == "https://[shop.example/x"
