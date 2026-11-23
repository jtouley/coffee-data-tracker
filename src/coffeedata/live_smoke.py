"""The deploy's smoke test and traffic move (deploy.yml): a new Cloud Run revision gets traffic only after it answers.

    python3 -m coffeedata.live_smoke rollout app|admin <service> --region R --tag T --live URL -- <gcloud run deploy args>
    python3 -m coffeedata.live_smoke check app|admin <base url>

`rollout` deploys the revision with no traffic and a tag of its own (`--no-traffic --tag`), smoke-tests the tag's
run.app URL, and only if that passes moves the traffic to it (`update-traffic --to-latest`). Then it checks the
live domain, and if that fails moves the traffic back to the revision that served before. Each outcome is one line in
the job summary. It needs no more than the deploy account already holds (Cloud Run Developer on the service: deploy,
describe, update traffic and tags), and it reads only public pages, signed out: there are no credentials to log.

**Pinned traffic stays pinned** (docs/app/RUNNING.md, Rolling back). If the newest revision didn't have 100% of the
traffic before this deploy, a person sent it elsewhere: the new revision is smoke-tested but gets no traffic, and the
run says so with a warning. The one exception is a hold this module left itself. Before the smoke test, the revision
still serving is tagged `held`, and the tag comes off only once the new revision has the traffic and the live domain
answers. While `held` is on the revision with all the traffic (a smoke test or live check failed, or the run was
cancelled halfway), the next deploy that passes moves the traffic as usual. A person who pins by hand while a hold is
on removes the tag (`--remove-tags held`; RUNNING.md says so), and then the pin is theirs again.

`check` runs one service's checks against a base URL and exits 1 if any fails. Each check retries with backoff
(transient 5xx from a revision that is still warming up), within one deadline for the whole run. A failure prints the
check, the status and what was wrong, never a page's body.

Standard library only: it runs on the runner's python3, before (and without) the project's dependencies.
"""

from __future__ import annotations

import argparse
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
from urllib.parse import urlsplit

HELD = "held"  # the tag a failed rollout leaves on the revision still serving
DEADLINE = 180.0  # seconds for one service's checks, retries included
REQUEST_TIMEOUT = 30.0  # one request: the first can wake an idle instance
BACKOFF = (2.0, 5.0, 10.0)  # waits between a check's attempts: four tries in all

# (status, headers with lowercase names, body) for a GET; `follow` follows redirects.
Fetch = Callable[[str, bool], tuple[int, dict[str, str], bytes]]


@dataclass(frozen=True)
class Check:
    name: str
    path: str
    status: int
    contains: str | None = None  # a regular expression the body must match
    location: str | None = None  # the path a redirect must point at
    follow: bool = False  # follow redirects, then check the final answer


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


def problem(check: Check, status: int, headers: dict[str, str], body: bytes) -> str | None:
    """What's wrong with this answer, or None."""
    if status != check.status:
        return f"status {status}, expected {check.status}"
    if check.location is not None:
        where = urlsplit(headers.get("location", "")).path
        if where != check.location:
            return f"redirects to {where or 'nowhere'!r}, expected {check.location!r}"
    if check.contains is not None and not re.search(check.contains, body.decode("utf-8", "replace").strip()):
        return f"the body doesn't match {check.contains!r}"
    return None


def run_checks(
    kind: str,
    base: str,
    get: Fetch = fetch,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline: float = DEADLINE,
) -> list[str]:
    """Run `kind`'s checks against `base`; print one line each; return the failures (empty: all passed)."""
    base = base.rstrip("/")
    end = clock() + deadline
    failures: list[str] = []
    checks = list(CHECKS[kind])
    found_css = False
    while checks:
        check = checks.pop(0)
        why: str | None = "not tried: out of time"
        for attempt in range(len(BACKOFF) + 1):
            if clock() >= end:
                break
            try:
                status, headers, body = get(base + check.path, check.follow)
                why = problem(check, status, headers, body)
            except (OSError, ValueError) as e:  # refused, reset, timed out, bad TLS
                why, body = f"no answer ({type(e).__name__})", b""
            if why is None:
                break
            if attempt < len(BACKOFF):
                sleep(min(BACKOFF[attempt], max(0.0, end - clock())))
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
                checks.insert(0, Check("stylesheet", m.group(1), 200))
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
    g = gcloud or Gcloud(service, region)
    before = g.describe()
    print(
        f"Before: {before.latest_percent}% follows the newest revision; serving {before.serving or 'nothing'}."
    )
    g("deploy", service, *deploy_args, "--no-traffic", "--tag", tag)
    after = g.describe()
    previous = after.sole  # what keeps serving until the traffic moves (None if split)
    # Before anything else can fail, so a run that fails, times out or is cancelled from here on leaves a hold the
    # next deploy can lift, not what looks like a person's pin. A person's pin gets no tag: it stays theirs.
    if not before.pinned and previous and not after.held:
        g.traffic("--update-tags", f"{HELD}={previous}")
    if tag not in after.tags or not after.tags[tag][1]:
        summary(
            f"**{service}: not checked.** Cloud Run gave the new revision no URL for the tag `{tag}`; it serves no one."
        )
        print(f"::error::{service}: Cloud Run gave the new revision no URL for the tag {tag}.")
        return 1
    revision, url = after.tags[tag]

    failures = check(kind, url)
    if failures:
        stays = f"`{previous}`" if previous else "the revisions it was on"
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

    g.traffic("--to-latest")
    live_failures = check(kind, live) if live else []
    if live_failures:
        failed = "; ".join(live_failures)
        if previous:
            g.traffic(
                f"--to-revisions={previous}=100"
            )  # the hold set above stays: the next good deploy lifts it
            summary(f"**{service}: rolled back.** `{revision}` passed its smoke test, but {live} then failed ({failed}), "
                    f"so the traffic is back on `{previous}`. The next deploy that passes goes live.")  # fmt: skip
        else:
            summary(f"**{service}: {live} failed after the traffic moved to `{revision}`** ({failed}), and no single "
                    "revision served before it. Roll back by hand: RUNNING.md, Rolling back.")  # fmt: skip
        print(f"::error::{service}: {live} failed its check on {revision}; see the job summary.")
        return 1
    g.traffic("--remove-tags", f"{HELD},{tag}" if previous else tag)
    summary(f"**{service}: smoke test passed on `{revision}`, which now has all the traffic**"
            + (f"; {live} answers." if live else "."))  # fmt: skip
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
    c.add_argument("kind", choices=CHECKS)
    c.add_argument("url")
    r = sub.add_parser("rollout")
    r.add_argument("kind", choices=CHECKS)
    r.add_argument("service")
    r.add_argument("--region", required=True)
    r.add_argument(
        "--tag", required=True, help="the new revision's tag: lowercase letters, digits and dashes"
    )
    r.add_argument("--live", help="the live domain, checked after the traffic moves")
    a = p.parse_args(argv)
    if a.cmd == "check":
        return 1 if run_checks(a.kind, a.url) else 0
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,24}", a.tag):
        p.error("--tag: a lowercase letter, then up to 24 lowercase letters, digits or dashes")
    if not deploy_args:
        p.error("rollout: the gcloud run deploy arguments go after --")
    return rollout(a.kind, a.service, a.region, a.tag, a.live, deploy_args)


if __name__ == "__main__":
    sys.exit(main())
