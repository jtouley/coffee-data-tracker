"""The deploy's smoke test and traffic move (src/coffeedata/live_smoke.py, deploy.yml): the checks pass on the real
app and admin and catch a broken one; a new revision gets traffic only after it passes; pinned traffic stays pinned;
a failed live check moves the traffic back; and the workflow runs it for both services."""

import http.client
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from coffeedata import live_smoke
from coffeedata.live_smoke import HELD, Traffic, rollout, run_checks, traffic

from .lakehelp import needs_dbt, product
from .lakehelp import run as crawl
from .test_admin import _app as admin_app
from .test_admin import build_lake as admin_lake
from .test_invites import accounts, app  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]


def via(asgi, base="https://candidate.example"):
    """live_smoke's fetch, answered by an app in this process instead of the network."""
    clients = {f: TestClient(asgi, base_url=base, follow_redirects=f) for f in (False, True)}

    def get(url: str, follow: bool):
        assert url.startswith(base), url
        r = clients[follow].get(url[len(base) :])
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content

    return base, get


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


# --- the checks --------------------------------------------------------------------------------------------------


def test_the_app_checks_pass_on_the_real_app_signed_out(app, capsys):  # noqa: F811
    base, get = via(app)
    assert run_checks("app", base, get=get, sleep=pytest.fail) == []
    out = capsys.readouterr().out
    assert out.count("ok  ") == len(live_smoke.APP) + 1, out  # and the stylesheet the sign-in page links
    assert "stylesheet (/static/" in out


def test_the_admin_check_passes_on_the_real_admin_sign_in_page(tmp_path):
    admin_lake(tmp_path / "lake")
    base, get = via(admin_app(tmp_path, tmp_path / "lake"))
    assert run_checks("admin", base, get=get, sleep=pytest.fail) == []


def test_a_broken_page_fails_after_retries_with_backoff_and_prints_no_body(app, capsys):  # noqa: F811
    base, real = via(app)
    calls = []

    def get(url, follow):
        calls.append(url)
        if url.endswith("/join"):
            return 500, {}, b"Traceback: secret-ish detail"
        return real(url, follow)

    clock = Clock()
    failures = run_checks("app", base, get=get, sleep=clock.sleep, clock=clock)
    assert failures == ["join page: status 500, expected 200"]
    assert calls.count(base + "/join") == len(live_smoke.BACKOFF) + 1
    assert clock.slept == list(live_smoke.BACKOFF)
    assert "Traceback" not in capsys.readouterr().out


def test_wrong_content_and_a_wrong_redirect_fail():
    page = live_smoke.Check(
        "sign-in page", "/signin", 200, contains=r"<title>Sign in · registry\.coffee</title>"
    )
    assert live_smoke.problem(page, 200, {}, b"<title>Oops</title>")
    assert live_smoke.problem(page, 200, {}, "<title>Sign in · registry.coffee</title>".encode()) is None
    gate = live_smoke.APP[3]
    assert (
        gate.path == "/"
        and live_smoke.problem(gate, 303, {"location": "https://x.run.app/signin"}, b"") is None
    )
    assert live_smoke.problem(gate, 303, {"location": "/"}, b"")  # a loop, not the gate
    assert live_smoke.problem(gate, 200, {}, b"")  # the gate is off


def test_the_whole_run_has_one_deadline():
    clock = Clock()

    def get(url, follow):
        clock.now += 50  # every request hangs most of its timeout
        raise TimeoutError

    failures = run_checks("app", "https://x", get=get, sleep=clock.sleep, clock=clock)
    assert len(failures) == len(live_smoke.APP) and clock.now <= live_smoke.DEADLINE + 50
    assert all("no answer (TimeoutError)" in f or "out of time" in f for f in failures)


# --- traffic and the rollout ---------------------------------------------------------------------------------------


class FakeGcloud:
    """The calls rollout makes, on a service whose traffic follows Cloud Run's rules as gcloud applies them:
    `--no-traffic` pins what the newest revision had to the newest *ready* revision by name, in the same update that
    creates the new revision (so even when it never starts), and `--to-latest` sends all of it to the newest ready
    one. `deploy_fails`: "after-pin" makes a revision that never starts, "before" fails before any update."""

    def __init__(self, pinned_to: str | None = None, held: bool = False, deploy_fails: str | None = None):
        self.revisions, self.ready = ["svc-00001", "svc-00002"], ["svc-00001", "svc-00002"]
        self.spec = (
            [{"revisionName": pinned_to, "percent": 100}]
            if pinned_to
            else [{"latestRevision": True, "percent": 100}]
        )
        self.tags = {HELD: pinned_to} if held else {}
        self.deploy_fails = deploy_fails
        self.calls: list[tuple[str, ...]] = []

    def describe(self) -> Traffic:
        status = [
            {"revisionName": t.get("revisionName") or self.ready[-1], "percent": t["percent"],
             **({"latestRevision": True} if t.get("latestRevision") else {})}
            for t in self.spec
        ] + [{"revisionName": r, "tag": t, "url": f"https://{t}---svc.run.app"} for t, r in self.tags.items()]  # fmt: skip
        return traffic({"spec": {"traffic": self.spec}, "status": {"traffic": status}})

    def __call__(self, *args):
        self.calls.append(args)
        assert args[0] == "deploy"
        if self.deploy_fails == "before":
            raise subprocess.CalledProcessError(1, "gcloud")
        if "--no-traffic" in args:
            self.spec = [t if not t.get("latestRevision") else {"revisionName": self.ready[-1], "percent": t["percent"]}
                         for t in self.spec]  # fmt: skip
        self.revisions.append(f"svc-{len(self.revisions) + 1:05d}")
        if "--tag" in args:
            self.tags[args[args.index("--tag") + 1]] = self.revisions[-1]
        if self.deploy_fails == "after-pin":
            raise subprocess.CalledProcessError(1, "gcloud")  # the revision never became ready
        self.ready.append(self.revisions[-1])

    def traffic(self, *args):
        self.calls.append(("update-traffic", *args))
        for a in args:
            if a == "--to-latest":
                self.spec = [{"latestRevision": True, "percent": 100}]
            elif a.startswith("--to-revisions="):
                rev, pct = a.split("=", 1)[1].split("=")
                self.spec = [{"revisionName": rev, "percent": int(pct)}]
        if "--update-tags" in args:
            k, v = args[args.index("--update-tags") + 1].split("=")
            self.tags[k] = v
        if "--remove-tags" in args:
            for k in args[args.index("--remove-tags") + 1].split(","):
                self.tags.pop(k, None)

    def serving(self) -> dict[str, int]:
        return self.describe().serving


def go(g, results, tmp_path, monkeypatch, live="https://app.example"):
    """rollout with the smoke test answering `results` in turn (a list of failures, or an exception, per call)."""
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    for f in ("summary.md", "output"):
        (tmp_path / f).unlink(missing_ok=True)
    seen = []

    def check(kind, url):
        seen.append(url)
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    code = rollout(
        "app", "svc", "r", "deploy-7-1", live, ["--image", "img@sha256:" + "a" * 64], gcloud=g, check=check
    )
    out = (tmp_path / "output").read_text() if (tmp_path / "output").exists() else ""
    return code, seen, (tmp_path / "summary.md").read_text(), out


def test_a_passing_revision_gets_the_traffic_only_after_its_smoke_test(tmp_path, monkeypatch):
    g = FakeGcloud()
    code, seen, summary, out = go(g, [[], []], tmp_path, monkeypatch)
    assert code == 0 and seen == ["https://deploy-7-1---svc.run.app", "https://app.example"]
    hold, deploy, move, untag = g.calls
    assert hold == ("update-traffic", "--update-tags", f"{HELD}=svc-00002")  # before the deploy
    assert deploy[:4] == ("deploy", "svc", "--image", "img@sha256:" + "a" * 64)
    assert deploy[-3:] == ("--no-traffic", "--tag", "deploy-7-1")
    assert move == ("update-traffic", "--to-latest")
    assert untag == ("update-traffic", "--remove-tags", f"{HELD},deploy-7-1")
    assert g.serving() == {"svc-00003": 100} and g.tags == {}
    assert "smoke test passed on `svc-00003`, which now has all the traffic" in summary
    assert out == "revision=svc-00003\n"  # for the admin's job


@pytest.mark.parametrize("when", ["after-pin", "before"])
def test_a_failed_deploy_leaves_the_workflows_own_hold_and_the_next_deploy_lifts_it(
    when, tmp_path, monkeypatch
):
    g = FakeGcloud(deploy_fails=when)
    code, seen, summary, out = go(g, [], tmp_path, monkeypatch)
    assert code == 1 and seen == [] and out == ""
    assert (
        "the deploy failed (CalledProcessError); the new revision gets no traffic.** Production stays on `svc-00002`"
        in summary
    )
    assert g.serving() == {"svc-00002": 100} and g.tags == {
        HELD: "svc-00002"
    }  # its tag dropped, the hold kept
    t = g.describe()
    assert t.held and not t.pinned  # never read as a person's pin
    assert t.latest_percent == (0 if when == "after-pin" else 100)

    g.deploy_fails = None
    code, _, summary, _ = go(g, [[], []], tmp_path, monkeypatch)
    assert code == 0 and ("update-traffic", "--to-latest") in g.calls
    assert g.describe().latest_percent == 100 and g.describe().sole == g.ready[-1] and g.tags == {}


def test_a_failing_revision_gets_no_traffic_no_url_and_the_next_good_deploy_lifts_the_hold(
    tmp_path, monkeypatch
):
    g = FakeGcloud()
    code, _, summary, _ = go(g, [["join page: status 500, expected 200"]], tmp_path, monkeypatch)
    assert code == 1 and not any("--to-latest" in c for c in g.calls)
    assert g.serving() == {"svc-00002": 100} and g.tags == {
        HELD: "svc-00002"
    }  # no public URL left on svc-00003
    assert (
        "smoke test failed on `svc-00003`, so it gets no traffic.** Production stays on `svc-00002`"
        in summary
    )
    assert "join page: status 500" in summary
    assert g.describe().held and not g.describe().pinned

    g.calls.clear()
    code, _, _, _ = go(g, [[], []], tmp_path, monkeypatch)
    assert code == 0 and ("update-traffic", "--to-latest") in g.calls
    assert not any("--update-tags" in c for c in g.calls)  # already held
    assert g.serving() == {"svc-00004": 100} and g.tags == {}


def test_pinned_traffic_stays_pinned_gets_no_hold_and_keeps_no_tag(tmp_path, monkeypatch, capsys):
    g = FakeGcloud(pinned_to="svc-00001")  # a person's rollback (RUNNING.md)
    code, seen, summary, out = go(g, [[]], tmp_path, monkeypatch)
    assert code == 0 and len(seen) == 1 and out == ""  # still smoke-tested; nothing made live
    assert [c[:2] for c in g.calls] == [("deploy", "svc"), ("update-traffic", "--remove-tags")]
    assert g.serving() == {"svc-00001": 100} and g.tags == {}
    assert "::warning::svc's traffic is pinned (0% to the newest revision)" in capsys.readouterr().out
    assert "but it serves no one" in summary

    code, _, summary, _ = go(g, [["health: status 503, expected 200"]], tmp_path, monkeypatch)
    assert code == 1 and g.tags == {} and g.serving() == {"svc-00001": 100}
    assert "Traffic was pinned before this deploy, and stays pinned." in summary


def test_a_person_who_pins_elsewhere_during_a_hold_keeps_the_pin():
    g = FakeGcloud(pinned_to="svc-00001", held=True)
    assert g.describe().held and not g.describe().pinned
    g.tags[HELD] = "svc-00002"  # held on one revision, the traffic on another: a person moved it
    assert g.describe().pinned


@pytest.mark.parametrize(
    "live", [["health: no answer (TimeoutError)"], http.client.IncompleteRead(b""), RuntimeError("bug")]
)
def test_anything_failing_after_the_move_moves_the_traffic_back(live, tmp_path, monkeypatch):
    g = FakeGcloud()
    code, _, summary, out = go(g, [[], live], tmp_path, monkeypatch)
    assert code == 1 and out == ""
    assert ("update-traffic", "--to-revisions=svc-00002=100") in g.calls
    assert g.serving() == {"svc-00002": 100} and g.tags == {HELD: "svc-00002"}
    assert "rolled back.** `svc-00003` passed its smoke test, but https://app.example then failed" in summary


def test_a_failed_move_keeps_production_and_says_so(tmp_path, monkeypatch):
    class MoveFails(FakeGcloud):
        def traffic(self, *args):
            if "--to-latest" in args:
                raise subprocess.CalledProcessError(1, "gcloud")
            super().traffic(*args)

    g = MoveFails()
    code, seen, summary, _ = go(g, [[]], tmp_path, monkeypatch)
    assert code == 1 and len(seen) == 1
    assert g.serving() == {"svc-00002": 100} and g.describe().held
    assert "not moved.** `svc-00003` passed its smoke test, but moving the traffic to it failed" in summary


def test_the_checks_count_a_garbled_answer_as_a_failure_not_a_crash():
    def get(url, follow):
        raise http.client.IncompleteRead(b"")

    clock = Clock()
    failures = run_checks("admin", "https://x", get=get, sleep=clock.sleep, clock=clock)
    assert failures == ["sign-in page: no answer (IncompleteRead)"]


def test_no_tag_url_fails_before_any_check(tmp_path, monkeypatch):
    class NoTagURL(FakeGcloud):
        def __call__(self, *args):
            super().__call__(*args)
            self.tags.pop("deploy-7-1")

    g = NoTagURL()
    code, seen, summary, _ = go(g, [], tmp_path, monkeypatch)
    assert code == 1 and seen == [] and "not checked" in summary and g.tags == {HELD: "svc-00002"}


def test_the_admin_follows_the_app_revision_this_run_made_live():
    def t(**kw):
        g = FakeGcloud(**kw)
        return g.describe()

    assert live_smoke.app_serves(t(), "svc-00002") == (True, "")
    # A later run failed its smoke test and left the app held here: still this run's, so the admin catches up.
    assert live_smoke.app_serves(t(pinned_to="svc-00002", held=True), "svc-00002")[0]
    ok, why = live_smoke.app_serves(t(), "svc-00001")  # a later run made svc-00002 live
    assert not ok and why.startswith("::notice::") and "svc-00002" in why
    ok, why = live_smoke.app_serves(t(pinned_to="svc-00001"), "svc-00002")  # a person's rollback
    assert not ok and why.startswith("::warning::") and "pinned" in why


def test_traffic_reads_a_split_and_tags_from_gclouds_json():
    svc = json.loads("""{"spec": {"traffic": [{"latestRevision": true, "percent": 90}, {"revisionName": "a", "percent": 10},
      {"revisionName": "a", "tag": "held"}]}, "status": {"traffic": [{"revisionName": "b", "percent": 90,
      "latestRevision": true}, {"revisionName": "a", "percent": 10}, {"revisionName": "a", "tag": "held",
      "url": "https://held---svc.run.app"}]}}""")
    t = traffic(svc)
    assert t.latest_percent == 90 and t.serving == {"b": 90, "a": 10} and t.sole is None
    assert t.tags == {"held": ("a", "https://held---svc.run.app")} and t.pinned and not t.held


# --- the workflow ------------------------------------------------------------------------------------------------


def _steps(job: str) -> list[dict]:
    return yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())["jobs"][job]["steps"]


@pytest.mark.parametrize(("job", "kind", "live"), [("deploy", "app", "https://app.registry.coffee"),
                                                   ("deploy-admin", "admin", "https://admin.registry.coffee")])  # fmt: skip
def test_both_services_deploy_through_the_smoke_test(job, kind, live):
    steps = _steps(job)
    (i,) = [n for n, s in enumerate(steps) if "coffeedata.live_smoke rollout" in s.get("run", "")]
    step = steps[i]
    assert f"live_smoke rollout {kind} " in step["run"] and f"--live {live} " in step["run"]
    assert step["env"]["PYTHONPATH"] == "src"
    assert re.fullmatch(
        r"deploy-\$\{\{ github\.run_id \}\}-\$\{\{ github\.run_attempt \}\}", step["env"]["TAG"]
    )
    assert any("actions/checkout@" in s.get("uses", "") for s in steps[:i])  # the module is in the checkout
    # Every deploy goes through it: no plain `gcloud run deploy` gives a revision traffic unchecked.
    assert not any("gcloud run deploy" in s.get("run", "") for s in steps)


def test_the_admin_checks_out_before_signing_in_and_follows_the_apps_revision():
    steps = _steps("deploy-admin")
    uses = [s.get("uses", "").split("@")[0] for s in steps]
    assert uses.index("actions/checkout") < uses.index("google-github-actions/auth")
    (tip,) = [s for s in steps if s.get("id") == "tip"]
    assert (
        "live_smoke app-serves" in tip["run"]
        and tip["env"]["REVISION"] == "${{ needs.deploy.outputs.revision }}"
    )
    jobs = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())["jobs"]
    assert jobs["deploy"]["outputs"]["revision"] == "${{ steps.rollout.outputs.revision }}"


def test_a_site_check_with_nothing_to_check_against_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    assert (
        live_smoke.main(["check", "site", "https://registry.coffee", "--expect", "", "--nonce", "1-1"]) == 1
    )
    assert "Site: not checked.**" in (tmp_path / "summary.md").read_text()
    assert "::error::" in capsys.readouterr().out


def test_every_action_in_the_deploy_workflow_is_pinned_by_commit():
    text = (ROOT / ".github/workflows/deploy.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses and all(u.startswith("./") or re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses


def test_runs_on_the_runners_python_without_the_projects_dependencies():
    code = (ROOT / "src/coffeedata/live_smoke.py").read_text()
    imports = set(re.findall(r"^(?:from|import) (\w+)", code, re.M))
    assert imports <= {"__future__", "argparse", "json", "os", "re", "subprocess", "sys", "time", "urllib",
                       "collections", "dataclasses", "pathlib", "http"}, imports  # fmt: skip


def test_fetch_reports_a_redirect_instead_of_following_it_unless_asked(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            code, extra = {"/": (303, {"Location": "/signin"}), "/api": (401, {})}.get(self.path, (200, {}))
            self.send_response(code)
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(self.path.encode())

        def log_message(self, *a):
            pass

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, headers, _ = live_smoke.fetch(base + "/", False)
        assert status == 303 and headers["location"] == "/signin"
        assert live_smoke.fetch(base + "/", True)[::2] == (200, b"/signin")
        assert live_smoke.fetch(base + "/api", False)[0] == 401
    finally:
        server.shutdown()
        server.server_close()


# --- the site (registry.coffee, GitHub Pages) ----------------------------------------------------------------------


def pages(folder: Path):
    """A static server like GitHub Pages: the folder's files, a query ignored, and Pages' own 404 page."""
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class Handler(SimpleHTTPRequestHandler):
        def send_error(self, code, message=None, explain=None):
            body = b"<title>Page not found &middot; GitHub Pages</title>" if code == 404 else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def built_site(lake, tmp_path):
    from coffeedata import site

    from .test_dbt import build

    crawl(lake, "2026-10-12", "onyxcoffeelab.com", [product("A"), product("B")])
    build(lake, tmp_path).close()
    return site.build(tmp_path / "coffee.duckdb", tmp_path / "site").parent


@needs_dbt
def test_the_site_checks_pass_on_the_deployed_build_and_catch_an_old_or_broken_one(
    built_site, tmp_path, monkeypatch
):
    import shutil

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    expect = live_smoke.expect_site(built_site)
    # (A local build has no release build id; the served one the workflows render always does.)
    assert (
        expect["data_key"]
        and expect["coffee_id"]
        and "build_id" in expect
        and expect["not_found_title"] is None
    )
    assert json.loads(json.dumps(expect)) == expect  # what the build job passes the smoke job, as one line
    live = tmp_path / "live"
    shutil.copytree(built_site, live)
    server, base = pages(live)
    try:
        assert live_smoke.check_site(base, expect, "7-1", sleep=pytest.fail) == 0
        assert (
            f"smoke test passed**: release `{expect['build_id']}` is live"
            in (tmp_path / "summary.md").read_text()
        )

        # Still the previous deploy: another release, another catalog page.
        data = (live / "catalog" / "catalog.json").read_text()
        built_id = '"release_build_id":' + json.dumps(expect["build_id"])
        assert built_id in data
        (live / "catalog" / "catalog.json").write_text(
            data.replace(built_id, '"release_build_id":"20261019T000000-0"')
        )
        page = (live / "catalog" / "index.html").read_text()
        (live / "catalog" / "index.html").write_text(page.replace(expect["data_key"], "0" * 12))
        shutil.rmtree(live / "privacy")  # and a page gone
        clock = Clock()
        assert live_smoke.check_site(base, expect, "7-1", sleep=clock.sleep, clock=clock) == 1
    finally:
        server.shutdown()
        server.server_close()
    summary = (tmp_path / "summary.md").read_text()
    assert "smoke test failed after the Pages deploy.** Failed: catalog page:" in summary
    assert "catalog download: the body doesn't match" in summary and "privacy page: status 404" in summary
    assert "home page" not in summary.split("Failed:")[-1] and "404 page" not in summary.split("Failed:")[-1]
    assert 'runbook/github-pages.md says ("When the smoke test fails")' in summary
    assert clock.slept[: len(live_smoke.SITE_BACKOFF)] == list(live_smoke.SITE_BACKOFF)


def test_every_site_request_skips_the_cdns_cached_copy():
    expect = {"home_title": "registry.coffee", "privacy_title": "Privacy", "data_key": "ab12", "build_id": "b1",
              "coffee_id": "c1", "not_found_title": None}  # fmt: skip
    checks = live_smoke.site_checks(expect, "42-1")
    assert {c.name for c in checks} == {
        "home page",
        "catalog page",
        "catalog download",
        "privacy page",
        "404 page",
    }
    assert all(c.bust == "42-1" for c in checks)
    seen = []

    def get(url, follow):
        seen.append(url)
        return 500, {}, b""

    clock = Clock()
    run_checks(
        "site", "https://site", get=get, sleep=clock.sleep, clock=clock, checks=checks[:1], backoff=(1.0, 1.0)
    )
    assert seen == ["https://site/?smoke=42-1-1", "https://site/?smoke=42-1-2", "https://site/?smoke=42-1-3"]
    assert all(c.contains for c in checks)  # a status and a content assertion each
    (missing,) = [c for c in checks if c.name == "404 page"]
    assert missing.status == 404


@pytest.mark.parametrize(("workflow", "build_job"), [("site.yml", "build"), ("weekly.yml", "run")])
def test_each_pages_deploy_is_followed_by_the_site_smoke_test(workflow, build_job):
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    steps = jobs[build_job]["steps"]
    names = [s.get("uses", "") or s.get("name", "") for s in steps]
    (upload,) = [i for i, n in enumerate(names) if n.startswith("actions/upload-pages-artifact@")]
    (expect,) = [i for i, s in enumerate(steps) if "live_smoke expect-site build/site" in s.get("run", "")]
    assert expect > upload  # a failure there never holds back the deploy
    assert steps[expect]["continue-on-error"] is True
    # Its own line, so errexit fails the step (an `echo "x=$(…)"` would hide the failure behind echo's 0).
    assert "expect=$(python3 -m coffeedata.live_smoke expect-site build/site)\n" in steps[expect]["run"]
    assert jobs[build_job]["outputs"]["smoke"] == "${{ steps.expect.outputs.site }}"
    smoke = jobs["smoke"]
    assert smoke["needs"] == [build_job, "deploy"] and "needs.deploy.result == 'success'" in smoke["if"]
    assert "outputs.smoke" not in smoke["if"]  # an empty expectation fails the job, never skips it
    assert smoke["permissions"] == {"contents": "read"}  # no Pages or id token
    (step,) = [s for s in smoke["steps"] if "live_smoke check site" in s.get("run", "")]
    assert step["env"]["EXPECT"] == f"${{{{ needs.{build_job}.outputs.smoke }}}}"
    assert "github.run_id" in step["env"]["NONCE"] and step["env"]["PYTHONPATH"] == "src"
    assert "registry.coffee" in step["env"]["SITE"]


def test_a_crashing_smoke_test_or_a_failed_hold_changes_nothing(tmp_path, monkeypatch):
    g = FakeGcloud()
    code, _, summary, _ = go(g, [RuntimeError("bug")], tmp_path, monkeypatch)
    assert code == 1 and g.serving() == {"svc-00002": 100} and g.tags == {HELD: "svc-00002"}
    assert "the smoke test failed (RuntimeError)" in summary

    class TagFails(FakeGcloud):
        def traffic(self, *args):
            raise subprocess.CalledProcessError(1, "gcloud")

    g = TagFails()
    code, seen, summary, _ = go(g, [], tmp_path, monkeypatch)
    assert code == 1 and seen == [] and not any(c[0] == "deploy" for c in g.calls)
    assert "not deployed.**" in summary
