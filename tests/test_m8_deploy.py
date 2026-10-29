"""The Cloud Run deploy: every write needs a signed-in account, the logs are JSON with a trace and no ids or
secrets, and the rate-limit address can't be spoofed behind Cloud Run's front end."""

import json
import logging
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import logs, web
from app.auth import LocalOwner, missing_secrets
from app.main import create_app
from coffeedata.lake import Lake

from .test_app import HeaderAuth, W, setup
from .test_invites import accounts, app, client, owner  # noqa: F401 - fixtures

PUBLIC_WRITES = {"/api/signin", "/api/join", "/api/reset", "/api/invites/check"}  # before an account exists


def test_every_write_route_refuses_a_request_without_an_account(app):  # noqa: F811
    c = client(app)
    writes = [
        (r.path, m)
        for r in app.routes
        for m in getattr(r, "methods", set()) or set()
        if m in {"POST", "PUT", "PATCH", "DELETE"} and r.path not in PUBLIC_WRITES
    ]
    assert (
        len(writes) >= 10
    )  # shots, retract, restore, bags, photos, equipment, invites, password, sign-out …
    for path, method in writes:
        url = re.sub(r"\{[^}]+\}", "x1", path)
        r = c.request(method, url, json={}, headers=W)
        assert r.status_code in (401, 403), f"{method} {path} answered {r.status_code} without an account"


def test_health_answers_on_a_path_googles_front_end_passes_through(app):  # noqa: F811
    # The uptime check goes through Google's front end, which answers any path ending in "z" itself; the startup
    # probe calls the container directly and keeps /healthz.
    c = client(app)
    for path in ("/health", "/healthz"):
        r = c.get(path)
        assert r.status_code == 200 and r.json() == {"ok": True}, path
    tf = (Path(__file__).parents[1] / "src/terraform/alerts.tf").read_text()
    assert re.search(r'path\s*=\s*"/health"', tf) and not re.search(r'path\s*=\s*"[^"]*z"', tf)


def test_signed_in_writes_only_reach_the_writers_own_data(lake):
    c = TestClient(create_app(lake, HeaderAuth()))
    setup(c)
    maya = W | {"x-test-user": "maya", "X-Expected-User": "maya"}
    # Maya can't log a shot with the owner's grinder, or retract the owner's shot.
    body = {"shot_id": "s1", "brewed_at": "2026-10-20T07:00:00Z", "grinder_id": "g1", "basket_id": "g1-basket",
            "dose_g": 18, "yield_g": 36, "time_s": 28, "grinder_setting": 11, "rating": 4}  # fmt: skip
    assert c.post("/api/shots", json=body, headers=W).status_code == 200
    assert c.post("/api/shots", json=body | {"shot_id": "s2"}, headers=maya).status_code == 404
    assert c.post("/api/shots/s1/retract", json={}, headers=maya).status_code == 404


def test_json_log_lines_carry_severity_and_the_trace(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    token = logs.REQUEST.set({"trace": logs.trace("0af7651916cd43dd8448eb211c80319c/1;o=1")})
    try:
        record = logging.LogRecord("app", logging.ERROR, __file__, 1, "boom %s", ("x",), None)
        try:
            raise ValueError("bad")
        except ValueError:
            import sys

            record.exc_info = sys.exc_info()
        line = json.loads(logs.JsonFormatter().format(record))
    finally:
        logs.REQUEST.reset(token)
    assert line["severity"] == "ERROR" and line["message"].startswith("boom x\nTraceback")
    assert line["logging.googleapis.com/trace"] == "projects/proj/traces/0af7651916cd43dd8448eb211c80319c"
    assert logs.trace("not a trace!/1") is None


def test_the_access_line_has_the_route_template_and_user_but_no_ids_or_query(lake, caplog):
    c = TestClient(create_app(lake, HeaderAuth()))
    setup(c)
    with caplog.at_level(logging.INFO, logger="app.access"):
        c.get("/shot/secret-shot-id?token=abc", headers={"x-test-user": "maya", "Accept": "text/html"})
    (rec,) = [r for r in caplog.records if r.name == "app.access"]
    assert rec.fields["route"] == "/shot/{shot_id}" and rec.fields["status"] == 404
    assert rec.fields["user_id"] == "maya" and rec.fields["latency_ms"] >= 0
    text = json.dumps(rec.fields) + rec.getMessage()
    assert "secret-shot-id" not in text and "token" not in text and "abc" not in text


def test_writes_log_what_was_saved_and_how_long_publishing_took(lake, caplog):
    c = TestClient(create_app(lake, HeaderAuth()))
    with caplog.at_level(logging.INFO, logger="app.events"):
        setup(c)
    saved = [r.fields for r in caplog.records if r.name == "app.events" and r.getMessage() == "saved"]
    assert saved and saved[0]["table"] == "equipment" and "publish_ms" in saved[0]


def test_the_rate_limit_address_is_the_one_cloud_run_appended():
    # Cloud Run's front end appends the caller's address; anything to its left came from the client.
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")],
        "client": ("169.254.1.1", 1),
    }
    assert web.client_ip(Request(scope), 1) == "203.0.113.9"
    scope["headers"] = [(b"x-forwarded-for", b"203.0.113.9")]
    assert web.client_ip(Request(scope), 1) == "203.0.113.9"


def test_cloud_run_counts_as_hosted(monkeypatch):
    for k in ("SPACE_ID", "SESSION_SECRET", "AUTH_PEPPER", "HF_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("K_SERVICE", "registry-coffee")
    assert set(missing_secrets()) == {"AUTH_PEPPER", "SESSION_SECRET", "HF_TOKEN"}
    with pytest.raises(RuntimeError):
        LocalOwner()


def test_secrets_stored_with_a_trailing_newline_still_work(monkeypatch, tmp_path):
    # `gcloud secrets versions add NAME --data-file=-` keeps the newline that ends a pasted or piped value.
    from app.auth import SECRETS

    values = {"HF_TOKEN": "hf_x", "ANTHROPIC_API_KEY": "sk-x", "AUTH_PEPPER": "p" * 20, "SESSION_SECRET": "s" * 40,
              "OWNER_SETUP_CODE": "SETUP-CODE-9"}  # fmt: skip
    for name in SECRETS:
        monkeypatch.setenv(name, values[name] + "\n")
    monkeypatch.setenv("K_SERVICE", "registry-coffee")
    monkeypatch.setenv("COFFEEDATA_USERS", str(tmp_path / "users"))
    hosted_app = create_app(lake=Lake(str(tmp_path / "lake")))

    assert all(os.environ[name] == values[name] for name in SECRETS)
    c = TestClient(hosted_app, base_url="https://testserver")
    r = c.post(
        "/api/signin",
        json={"setup_code": "SETUP-CODE-9", "username": "jtouley", "password": "kettle orbit marble"},
        headers=W,
    )
    assert r.status_code == 200, r.text  # the setup code matches as typed
