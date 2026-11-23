"""The deploys' smoke tests: a new Cloud Run revision gets traffic only after it answers (deploy.yml), and the
public site is checked once GitHub Pages has it (site.yml, weekly.yml).

    python3 -m coffeedata.live_smoke rollout app|admin <service> --region R --tag T --live URL -- <gcloud run deploy args>
    python3 -m coffeedata.live_smoke check app|admin <base url>
    python3 -m coffeedata.live_smoke expect-site <built site folder>        (prints one line of JSON)
    python3 -m coffeedata.live_smoke check site <base url> --expect <that JSON> --nonce <run id>
    python3 -m coffeedata.live_smoke app-serves <app service> --region R --revision <name>    (for the admin's deploy)

`rollout` deploys the revision with no traffic and a tag of its own (`--no-traffic --tag`), smoke-tests the tag's
run.app URL, and only if that passes moves the traffic to it (`update-traffic --to-latest`). Then it checks the
live domain, and if that fails moves the traffic back to the revision that served before. Each outcome is one line in
the job summary. It needs no more than the deploy account already holds (Cloud Run Developer on the service: deploy,
describe, update traffic and tags), and it reads only public pages, signed out: there are no credentials to log.

**Pinned traffic stays pinned** (docs/app/RUNNING.md, Rolling back). If the newest revision didn't have 100% of the
traffic before this deploy, a person sent it elsewhere: the new revision is smoke-tested but gets no traffic, and the
run says so with a warning. The one exception is a hold this module left itself. Before the deploy (whose
`--no-traffic` pins the serving revision by name, even if the new one never starts), the revision serving is tagged
`held`; the tag comes off only once the new revision has the traffic and the live domain answers. While `held` is on
the revision with all the traffic (a deploy, smoke test or live check failed, or the run stopped halfway), the next
deploy that passes moves the traffic as usual. A person who pins by hand removes the tag in the same command
(`--remove-tags held`, as RUNNING.md's rollback does), and then the pin is theirs.

A new revision that fails, or that won't serve because traffic is pinned, loses its tag, so it has no public URL of
its own; anything that goes wrong after the traffic moved sends it back to the revision that served before. Each
outcome, failures included, is one line in the job summary.

**The site** (registry.coffee) is static, on GitHub Pages: there is no traffic to move. `expect-site` reads what was
just built (the landing and privacy pages' titles, the catalog page's data key, the release's build id and one
coffee's id in catalog.json), and `check site` asks the live site for each, plus the 404, after the deploy. Only the
catalog page and catalog.json tell this build from the one before; the other pages show the site answers. Every try
asks with a new `?smoke=<nonce>-<try>`, so Pages' CDN can't answer from a copy it cached (for up to ten minutes),
even one cached by an earlier try. A failure fails the run, and the summary says how to put the previous site back
(runbook/github-pages.md).

`check` runs one service's checks against a base URL and exits 1 if any fails. Each check retries with backoff
(transient 5xx from a revision that is still warming up), within one deadline for the whole run. A failure prints the
check, the status and what was wrong, never a page's body.

Standard library only: it runs on the runner's python3, before (and without) the project's dependencies.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

HELD = "held"  # on the serving revision while a rollout is under way, and after one that failed
DEADLINE = 180.0  # seconds for one service's checks, retries included
REQUEST_TIMEOUT = 30.0  # one request: the first can wake an idle instance
BACKOFF = (2.0, 5.0, 10.0)  # waits between a check's attempts: four tries in all
SITE_BACKOFF = (5.0, 15.0, 30.0, 60.0)  # a Pages deploy can take a minute to reach every edge

# (status, headers with lowercase names, body) for a GET; `follow` follows redirects.
Fetch = Callable[[str, bool], tuple[int, dict[str, str], bytes]]


@dataclass(frozen=True)
class Check:
    name: str
    path: str
    status: int
    contains: str | tuple[str, ...] | None = None  # regular expressions the body must match, each
    location: str | None = None  # the path a redirect must point at
    follow: bool = False  # follow redirects, then check the final answer
    bust: str | None = None  # asks with ?smoke=<bust>-<attempt>: a new URL each try, so no CDN has it cached


# The app (app.registry.coffee) answers nobody signed out but its sign-in pages (app/auth.py, PUBLIC), so these are
# what a signed-out client can see: health, the two pages people land on, the gate on a screen, on an unknown path
# and on the API, and the stylesheet every page links (a dropped static file leaves every page unstyled).
APP: tuple[Check, ...] = (
    Check("health", "/health", 200, contains=r'^\{"ok":\s*true\}$'),
    Check("sign-in page", "/signin", 200, contains=r"<title>(Sign in|Set up) · registry\.coffee</title>"),
    Check("join page", "/join", 200, contains=r"<title>Join · registry\.coffee</title>"),
    Check("home, signed out", "/", 303, location="/signin"),
    Check("unknown page, signed out", "/smoke-test-no-such-page", 303, location="/signin"),
    Check("API, signed out", "/api/coffees/search?q=smoke", 401, contains=r'"detail":\s*"sign in first"'),
)
# The admin (admin.registry.coffee): only its sign-in page, which sends a new admin on to /enroll. No credentials.
ADMIN: tuple[Check, ...] = (
    Check("sign-in page", "/signin", 200, contains=r"· admin\.registry\.coffee</title>", follow=True),
)
CHECKS = {"app": APP, "admin": ADMIN}
STYLESHEET = re.compile(r'<link rel="stylesheet" href="(/static/[^"]+\.css)"')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the 3xx comes back as an HTTPError, with its Location


def fetch(url: str, follow: bool) -> tuple[int, dict[str, str], bytes]:
    handlers = [] if follow else [_NoRedirect()]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "registry-coffee-deploy-smoke"})
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def problem(check: Check, status: int, headers: dict[str, str], body: bytes, host: str = "") -> str | None:
    """What's wrong with this answer, or None. A redirect must stay on `host` (a relative Location does)."""
    if status != check.status:
        return f"status {status}, expected {check.status}"
    if check.location is not None:
        loc = urlsplit(headers.get("location", ""))
        if loc.netloc and loc.netloc != host:
            return f"redirects to another host, {loc.netloc!r}"
        where = loc.path
        if where != check.location:
            return f"redirects to {where or 'nowhere'!r}, expected {check.location!r}"
    patterns = (check.contains,) if isinstance(check.contains, str) else check.contains or ()
    text = body.decode("utf-8", "replace").strip() if patterns else ""
    for pattern in patterns:
        if not re.search(pattern, text):
            return f"the body doesn't match {pattern!r}"
    return None


# --- the site --------------------------------------------------------------------------------------------------------

TITLE = re.compile(r"<title>([^<]*)</title>")
DATA_KEY = re.compile(r"catalog\.json\?b=([0-9a-f]+)")


def _title(page: Path) -> str:
    m = TITLE.search(page.read_text(encoding="utf-8"))
    if m is None:
        raise ValueError(f"{page} has no <title>")
    return m.group(1)


def expect_site(out: str | Path) -> dict[str, str | None]:
    """What the live site must serve once this build is deployed, read from the build (`coffeedata site`'s folder)."""
    out = Path(out)
    key = DATA_KEY.search((out / "catalog" / "index.html").read_text(encoding="utf-8"))
    data = json.loads((out / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    rows, cols = data.get("rows") or [], data.get("columns") or []
    coffee = rows[0][cols.index("coffee_id")] if rows and "coffee_id" in cols else None
    return {
        "home_title": _title(out / "index.html"),
        "privacy_title": _title(out / "privacy" / "index.html"),
        "data_key": key.group(1) if key else None,
        "build_id": data["release"]["release_build_id"],
        "coffee_id": coffee,
        "not_found_title": _title(out / "404.html") if (out / "404.html").exists() else None,
    }


def site_checks(expect: dict, nonce: str) -> tuple[Check, ...]:
    """The live site against `expect` (expect_site's): each page by its status and content. Only the catalog page
    (its data key) and the download (its release and one coffee) tell this build from the one before; the home and
    privacy titles show the site answers."""

    def title(t: str) -> str:
        return f"<title>{re.escape(t)}</title>"

    catalog: tuple[str, ...] = (
        r'^\{"release":\{',
        '"release_build_id":' + re.escape(json.dumps(expect["build_id"])),
    )
    if expect.get("coffee_id"):
        catalog += (re.escape(json.dumps(expect["coffee_id"])),)  # one coffee this build lists
    page = (
        (r"<title>[^<]*</title>", rf"catalog\.json\?b={expect['data_key']}")
        if expect.get("data_key")
        else None
    )
    return (
        Check("home page", "/", 200, contains=title(expect["home_title"]), bust=nonce),
        Check("catalog page", "/catalog/", 200, contains=page or r"<h1 class=\"serif\">The catalog", bust=nonce),
        Check("catalog download", "/catalog/catalog.json", 200, contains=catalog, bust=nonce),
        Check("privacy page", "/privacy/", 200, contains=title(expect["privacy_title"]), bust=nonce),
        Check("404 page", "/smoke-test-no-such-page", 404, bust=nonce,
              contains=title(expect["not_found_title"]) if expect.get("not_found_title") else r"Page not found"),
    )  # fmt: skip


def run_checks(
    kind: str,
    base: str,
    get: Fetch = fetch,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline: float = DEADLINE,
    checks: tuple[Check, ...] | None = None,
    backoff: tuple[float, ...] = BACKOFF,
) -> list[str]:
    """Run `kind`'s checks (or `checks`) against `base`; print one line each; return the failures (empty: passed)."""
    base = base.rstrip("/")
    host = urlsplit(base).netloc
    end = clock() + deadline
    failures: list[str] = []
    pending = list(CHECKS[kind] if checks is None else checks)
    found_css = False
    while pending:
        check = pending.pop(0)
        why: str | None = "not tried: out of time"
        # Once one check has used up its retries, the rest get one try each: the warm-up time is spent, and one
        # stale or broken page mustn't use the whole deadline before the others are asked.
        waits = backoff if not failures else ()
        for attempt in range(len(waits) + 1):
            if clock() >= end:
                break
            url = base + check.path + (f"?smoke={check.bust}-{attempt + 1}" if check.bust else "")
            try:
                status, headers, body = get(url, check.follow)
                why = problem(check, status, headers, body, host)
            # refused, reset, timed out, bad TLS (OSError); a cut-off or garbled answer (HTTPException)
            except (OSError, ValueError, http.client.HTTPException) as e:
                why, body = f"no answer ({type(e).__name__})", b""
            if why is None:
                break
            if attempt < len(waits):
                sleep(min(waits[attempt], max(0.0, end - clock())))
        print(
            f"{'ok  ' if why is None else 'FAIL'} {check.name} ({check.path}){'' if why is None else ': ' + why}"
        )
        if why is not None:
            failures.append(f"{check.name}: {why}")
        elif kind == "app" and check.path == "/signin" and not found_css:
            # The page's own stylesheet, by the hashed name it links today.
            found_css = True
            m = STYLESHEET.search(body.decode("utf-8", "replace"))
            if m is None:
                print("FAIL stylesheet: the sign-in page links none")
                failures.append("stylesheet: the sign-in page links none")
            else:
                pending.insert(0, Check("stylesheet", m.group(1), 200))
    return failures


# --- traffic ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Traffic:
    latest_percent: int  # the share of traffic that follows the newest revision
    serving: dict[str, int]  # revision name -> percent, for each revision with any traffic
    tags: dict[str, tuple[str, str]]  # tag -> (revision name, its URL)

    @property
    def sole(self) -> str | None:
        """The revision with all the traffic, if one has it."""
        return next((r for r, p in self.serving.items() if p == 100), None)

    @property
    def held(self) -> bool:
        """All the traffic is on the revision a failed rollout left tagged `held`: the hold is this module's."""
        return HELD in self.tags and self.sole == self.tags[HELD][0]

    @property
    def pinned(self) -> bool:
        """A person sent the traffic somewhere other than the newest revision."""
        return self.latest_percent != 100 and not self.held


def traffic(svc: dict) -> Traffic:
    """From `gcloud run services describe --format=json` (Cloud Run's v1 shape, as the rest of deploy.yml reads it)."""
    spec = (svc.get("spec") or {}).get("traffic") or []
    status = (svc.get("status") or {}).get("traffic") or []
    latest = sum(int(t.get("percent") or 0) for t in spec if t.get("latestRevision"))
    serving: dict[str, int] = {}
    tags: dict[str, tuple[str, str]] = {}
    for t in status:
        name = t.get("revisionName") or ""
        if int(t.get("percent") or 0) > 0:
            serving[name] = serving.get(name, 0) + int(t["percent"])
        if t.get("tag"):
            tags[t["tag"]] = (name, t.get("url") or "")
    return Traffic(latest, serving, tags)


# --- rollout ---------------------------------------------------------------------------------------------------------


class Gcloud:
    def __init__(self, service: str, region: str):
        self.service, self.region = service, region

    def __call__(self, *args: str) -> str:
        cmd = ["gcloud", "run", *args, "--region", self.region, "--quiet"]
        return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, text=True, timeout=900).stdout

    def describe(self) -> Traffic:
        return traffic(json.loads(self("services", "describe", self.service, "--format=json")))

    def traffic(self, *args: str) -> None:
        self("services", "update-traffic", self.service, *args)


def summary(line: str) -> None:
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n\n")


def _quietly(what: str, call: Callable[..., object], *args: str) -> bool:
    """A tidy-up call whose failure mustn't hide the outcome: warn, and carry on."""
    try:
        call(*args)
        return True
    except Exception as e:  # noqa: BLE001 - reported, not raised
        print(
            f'::warning::Couldn\'t {what} ({type(e).__name__}); do it by hand: RUNNING.md, "A failed smoke test".'
        )
        return False


def _output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def rollout(
    kind: str,
    service: str,
    region: str,
    tag: str,
    live: str | None,
    deploy_args: list[str],
    gcloud: Gcloud | None = None,
    check: Callable[[str, str], list[str]] = run_checks,
) -> int:
    """Deploy, smoke-test, move the traffic (see the module's docstring). On success, writes `revision=<name>` to
    $GITHUB_OUTPUT: the revision this run made live."""
    g = gcloud or Gcloud(service, region)
    try:
        before = g.describe()
    except Exception as e:  # noqa: BLE001 - nothing changed yet
        summary(
            f"**{service}: not deployed.** Reading its traffic failed ({type(e).__name__}); nothing was changed."
        )
        print(f"::error::{service}: gcloud run services describe failed; nothing was deployed.")
        return 1
    print(
        f"Before: {before.latest_percent}% follows the newest revision; serving {before.serving or 'nothing'}."
    )
    previous = before.sole  # what serves now, and keeps serving until the traffic moves (None if split)
    stays = f"`{previous}`" if previous else "the revisions it was on"
    # Before the deploy: `--no-traffic` pins `previous` by name in the same update that makes the revision, even if
    # the revision never starts or the run stops while gcloud waits. Tagged first, that pin is the workflow's own,
    # and the next deploy lifts it. A person's pin gets no tag: it stays theirs.
    if not before.pinned and previous and not before.held:
        try:
            g.traffic("--update-tags", f"{HELD}={previous}")
        except Exception as e:  # noqa: BLE001 - nothing deployed yet
            summary(f"**{service}: not deployed.** Tagging the serving revision `{previous}` failed "
                    f"({type(e).__name__}), so nothing was deployed. Production stays on {stays}.")  # fmt: skip
            print(f"::error::{service}: couldn't tag {previous} {HELD}; nothing was deployed.")
            return 1

    def drop_tag(known: Traffic | None = None) -> None:
        """Remove this run's tag, if the service has it (`known`, or a fresh read; unreadable: try anyway)."""
        if known is None:
            try:
                known = g.describe()
            except Exception:  # noqa: BLE001 - can't tell: try the removal
                known = None
        if known is None or tag in known.tags:
            _quietly(f"remove the tag {tag}", g.traffic, "--remove-tags", tag)

    try:
        g("deploy", service, *deploy_args, "--no-traffic", "--tag", tag)
        after = g.describe()
    except Exception as e:  # noqa: BLE001 - summarised, then the run fails
        drop_tag()
        summary(f"**{service}: the deploy failed ({type(e).__name__}); the new revision gets no traffic.** "
                f"Production stays on {stays}.")  # fmt: skip
        print(f"::error::{service}: gcloud run deploy failed; production is unchanged.")
        return 1
    if tag not in after.tags or not after.tags[tag][1]:
        drop_tag(after)
        summary(f"**{service}: not checked.** Cloud Run gave the new revision no URL for the tag `{tag}`; it serves "
                f"no one. Production stays on {stays}.")  # fmt: skip
        print(f"::error::{service}: Cloud Run gave the new revision no URL for the tag {tag}.")
        return 1
    revision, url = after.tags[tag]

    try:
        failures = check(kind, url)
    except Exception as e:  # noqa: BLE001 - a crash in the check is a failed check
        failures = [f"the smoke test failed ({type(e).__name__})"]
    # A revision that failed, or that won't serve (pinned), keeps no public URL of its own.
    if failures or before.pinned:
        drop_tag(after)
    if failures:
        then = (
            "Traffic was pinned before this deploy, and stays pinned."
            if before.pinned
            else "The next deploy that passes goes live."
        )
        summary(f"**{service}: smoke test failed on `{revision}`, so it gets no traffic.** Production stays on {stays}. "
                f"Failed: {'; '.join(failures)}. {then}")  # fmt: skip
        print(
            f"::error::{service}: the new revision {revision} failed its smoke test; production is unchanged."
        )
        return 1
    if before.pinned:
        pinned = (f"{service}'s traffic is pinned ({before.latest_percent}% to the newest revision), so this deploy "
                  "serves no one. After a rollback, move it back: RUNNING.md, Rolling back.")  # fmt: skip
        summary(f"**{service}: smoke test passed on `{revision}`, but it serves no one.** {pinned}")
        print(f"::warning::{pinned}")
        return 0

    # From here the new revision may have the traffic: whatever goes wrong, the traffic goes back to `previous`.
    # `--to-latest` can apply and still fail (gcloud timing out while it waits), so once it was sent, the move back
    # always runs; it's idempotent.
    sent = moved = False
    try:
        sent = True
        g.traffic("--to-latest")
        moved = True
        live_failures = check(kind, live) if live else []
    except Exception as e:  # noqa: BLE001 - rolled back and summarised below
        live_failures = [f"{'the live check' if moved else 'moving the traffic'} failed ({type(e).__name__})"]
    if live_failures:
        failed = "; ".join(live_failures)
        back = bool(sent and previous) and _quietly(
            f"move the traffic back to {previous}", g.traffic, f"--to-revisions={previous}=100"
        )
        drop_tag(after)
        if back:  # the hold set above stays on `previous`: the next good deploy lifts it
            what = f"{live} then failed" if moved else "moving the traffic to it failed"
            summary(f"**{service}: rolled back.** `{revision}` passed its smoke test, but {what} ({failed}), so the "
                    f"traffic is back on `{previous}`. The next deploy that passes goes live.")  # fmt: skip
        else:
            summary(f"**{service}: `{revision}` may have the traffic.** {'Moving it' if not moved else live} failed "
                    f"({failed}), and moving the traffic back didn't work. Roll back by hand: RUNNING.md, Rolling "
                    "back.")  # fmt: skip
        print(f"::error::{service}: {failed} on {revision}; see the job summary.")
        return 1
    _quietly("remove the tags", g.traffic, "--remove-tags", f"{HELD},{tag}" if previous else tag)
    _output("revision", revision)
    summary(f"**{service}: smoke test passed on `{revision}`, which now has all the traffic**"
            + (f"; {live} answers." if live else "."))  # fmt: skip
    return 0


def app_serves(t: Traffic, revision: str) -> tuple[bool, str]:
    """For the admin's deploy: is the app's traffic all on `revision` (the one this run made live)? A hold counts
    (the app is still on it after a later deploy failed); a later deploy's revision or a person's pin doesn't."""
    if t.sole == revision:
        return True, ""
    if t.pinned:
        return False, (f"::warning::Not deploying to the admin: the app's traffic is pinned ({t.latest_percent}% to its "
                       "newest revision). Roll the admin back or forward with the app: RUNNING.md, Rolling back.")  # fmt: skip
    return False, (f"::notice::Not deploying to the admin: the app now serves {t.sole or t.serving}, not {revision}, "
                   "and the run that made that live deploys the admin.")  # fmt: skip


REDEPLOY = ("Pages has no traffic to move: put the previous site back as runbook/github-pages.md says "
            "(\"When the smoke test fails\").")  # fmt: skip


def check_site(base: str, expect: dict, nonce: str, **kw) -> int:
    """The live site after a Pages deploy; one summary line either way."""
    failures = run_checks("site", base, checks=site_checks(expect, nonce), backoff=SITE_BACKOFF, **kw)
    host = urlsplit(base).netloc or base
    if failures:
        summary(
            f"**{host}: smoke test failed after the Pages deploy.** Failed: {'; '.join(failures)}. {REDEPLOY}"
        )
        print(f"::error::{host} failed its smoke test after the deploy; see the job summary.")
        return 1
    summary(
        f"**{host}: smoke test passed**: release `{expect['build_id']}` is live (home, catalog, download, privacy, 404)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    deploy_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, deploy_args = argv[:i], argv[i + 1 :]
    p = argparse.ArgumentParser(prog="python3 -m coffeedata.live_smoke")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("kind", choices=[*CHECKS, "site"])
    c.add_argument("url")
    c.add_argument("--expect", help="site: expect-site's JSON")
    c.add_argument("--nonce", default=str(int(time.time())), help="site: the cache-busting query's value")
    e = sub.add_parser("expect-site")
    e.add_argument("folder")
    v = sub.add_parser("app-serves", help="writes current=true|false to $GITHUB_OUTPUT")
    v.add_argument("service")
    v.add_argument("--region", required=True)
    v.add_argument("--revision", default="")
    r = sub.add_parser("rollout")
    r.add_argument("kind", choices=CHECKS)
    r.add_argument("service")
    r.add_argument("--region", required=True)
    r.add_argument(
        "--tag", required=True, help="the new revision's tag: lowercase letters, digits and dashes"
    )
    r.add_argument("--live", help="the live domain, checked after the traffic moves")
    a = p.parse_args(argv)
    if a.cmd == "expect-site":
        print(json.dumps(expect_site(a.folder), separators=(",", ":")))
        return 0
    if a.cmd == "app-serves":
        try:
            ok, why = app_serves(Gcloud(a.service, a.region).describe(), a.revision) if a.revision else (
                False, "::warning::Not deploying to the admin: this run made no app revision live (its traffic is pinned).")  # fmt: skip
        except Exception as e:  # noqa: BLE001 - the admin isn't touched
            summary(
                f"**{a.service}: couldn't read the app's traffic ({type(e).__name__}), so the admin wasn't deployed.**"
            )
            print(f"::error::Reading {a.service}'s traffic failed; the admin wasn't deployed.")
            return 1
        if why:
            print(why)
        _output("current", "true" if ok else "false")
        return 0
    if a.cmd == "check" and a.kind == "site":
        if not a.expect:
            summary("**Site: not checked.** The build job couldn't record what it built (its \"Note what the smoke "
                    f"test expects\" step failed), so the site it deployed may be broken. {REDEPLOY}")  # fmt: skip
            print("::error::The site was deployed, but the build job recorded nothing to check it against.")
            return 1
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", a.nonce):
            p.error("--nonce: letters, digits and dashes")
        return check_site(a.url, json.loads(a.expect), a.nonce)
    if a.cmd == "check":
        return 1 if run_checks(a.kind, a.url) else 0
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,24}", a.tag):
        p.error("--tag: a lowercase letter, then up to 24 lowercase letters, digits or dashes")
    if not deploy_args:
        p.error("rollout: the gcloud run deploy arguments go after --")
    return rollout(a.kind, a.service, a.region, a.tag, a.live, deploy_args)


if __name__ == "__main__":
    sys.exit(main())
