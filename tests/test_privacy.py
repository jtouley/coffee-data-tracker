"""What the privacy policy (https://registry.coffee/privacy/) says the app does, checked against the app."""

import inspect
import json
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app import labels
from coffeedata import erase
from coffeedata.lake import PHOTO_TABLES, USER_TABLES, Lake

from .test_bags import LABEL, FakeClient
from .test_invites import PW, W, accounts, app, client, invite, join, owner  # noqa: F401 - fixtures

ROOT = Path(__file__).parent.parent


def every_value(lake: Lake) -> str:
    """Everything stored in a local lake, as text."""
    return " ".join(
        json.dumps(row, default=str)
        for f in lake.root.rglob("*.parquet")
        for row in pq.read_table(f).to_pylist()
    )


# 1. EXIF is removed from bag photos before they're stored: tests/test_bags.py
#    (test_photo_is_downsized_and_loses_its_metadata, test_photo_upload_reads_matches_and_stores_the_photo).


def test_ip_addresses_are_kept_in_memory_only_never_stored_or_logged(  # noqa: F811
    app,  # noqa: F811
    accounts,  # noqa: F811
    lake,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("COFFEEDATA_PROXY_HOPS", "1")
    ip = "203.0.113.77"
    with caplog.at_level(logging.DEBUG):
        c = client(app)
        h = W | {"X-Forwarded-For": ip}
        c.post(
            "/api/signin",
            json={"setup_code": "SETUP-CODE-1234", "username": "jtouley", "password": PW},
            headers=h,
        )
        c.post("/api/signin", json={"username": "jtouley", "password": "wrong password!"}, headers=h)
        c.get("/", headers=h)
    assert ip not in every_value(accounts.lake) and ip not in every_value(lake)
    assert not any(ip in r.getMessage() or ip in json.dumps(getattr(r, "fields", {})) for r in caplog.records)


def test_passwords_are_stored_only_as_a_hash(owner, accounts):  # noqa: F811
    stored = every_value(accounts.lake)
    assert PW not in stored and "$argon2id$" in stored


def test_the_app_uses_four_cookies(app, owner):  # noqa: F811
    # The server sets one cookie, the session. The page script sets three: theme and handedness (from Setup) and
    # the time zone, so times show in the phone's own. (The policy lists the first three; the time zone is the
    # fourth.)
    names = set()
    c = client(app)
    for r in (
        c.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=W),
        c.get("/"),
        c.get("/setup"),
        c.post("/api/signout", json={}, headers=W),
    ):
        names |= {h.split("=", 1)[0] for h in r.headers.get_list("set-cookie")}
    assert names == {"session"}
    js = (ROOT / "app/static/app.js").read_text()
    assert set(re.findall(r"document\.cookie = `(\w+)=", js)) == {"tz"}
    assert set(re.findall(r'preference\(\w+, "(\w+)"', js)) == {"theme", "hand"}
    assert js.count("document.cookie =") == 2  # tz, and the one in preference()


def test_anthropic_receives_only_the_photo_and_the_fixed_prompt():
    fake = FakeClient(LABEL)
    labels.read(b"\xff\xd8jpeg", labels.OPUS, fake)
    ((_, request),) = fake.calls
    assert set(request) <= {"model", "max_tokens", "output_config", "messages", "betas", "fallbacks"}
    (message,) = request["messages"]
    kinds = [part["type"] for part in message["content"]]
    assert kinds == ["image", "text"]
    assert message["content"][1]["text"] == labels.PROMPT.format(year=date.today().year)
    # The reader is never given who took the photo, and the request carries no metadata.
    assert set(inspect.signature(labels.read).parameters) == {"jpeg", "model_id", "client"}
    assert "metadata" not in request


# 6. Deleting a person's data on request.


@pytest.fixture
def three(tmp_path):
    main = Lake(str(tmp_path / "lake"))
    photos = Lake(str(tmp_path / "photos"), tables=PHOTO_TABLES)
    users = Lake(str(tmp_path / "users"), tables=USER_TABLES)
    t = datetime(2026, 10, 20, 7, tzinfo=UTC)
    for uid, name, role in (("owner", "jtouley", "owner"), ("u-maya", "maya", "member")):
        users.write("users", f"users/{uid}__20261020T070000.parquet",
                    [{"user_id": uid, "username": name, "role": role, "logged_at": t}])  # fmt: skip
        main.write("shots", f"shots/s-{uid}__20261020T070000.parquet",
                   [{"shot_id": f"s-{uid}", "user_id": uid, "logged_at": t, "brewed_at": t}])  # fmt: skip
        main.write(
            "bags",
            f"bags/b-{uid}__20261020T070000.parquet",
            [{"bag_id": f"b-{uid}", "user_id": uid, "logged_at": t}],
        )
        photos.write("photos", f"photos/p-{uid}.parquet", [{"photo_id": f"p-{uid}", "user_id": uid}])
    users.write("invites", "invites/i1__20261020T070000.parquet",
                [{"invite_id": "i1", "used_by": "u-maya", "created_by": "owner", "logged_at": t}])  # fmt: skip
    return main, photos, users


def test_delete_user_removes_their_files_from_every_dataset_and_nobody_elses(three):
    main, photos, users = three
    p = erase.plan("Maya", main, photos, users)
    assert p.user_id == "u-maya"
    assert p.files == {
        main.url: ["bags/b-u-maya__20261020T070000.parquet", "shots/s-u-maya__20261020T070000.parquet"],
        photos.url: ["photos/p-u-maya.parquet"],
        users.url: ["invites/i1__20261020T070000.parquet", "users/u-maya__20261020T070000.parquet"],
    }
    erase.apply(p, main, photos, users)
    left = every_value(main) + every_value(photos) + every_value(users)
    assert "u-maya" not in left and "s-owner" in left and "p-owner" in left


def test_delete_user_refuses_the_owner_and_unknown_names(three):
    with pytest.raises(erase.EraseError, match="owner"):
        erase.plan("jtouley", *three)
    with pytest.raises(erase.EraseError, match="No account"):
        erase.plan("nobody", *three)


def test_delete_user_dry_run_deletes_nothing(three, monkeypatch, capsys):
    from coffeedata import cli

    main, photos, users = three
    monkeypatch.setattr(cli.Lake, "photos", classmethod(lambda cls: photos))
    monkeypatch.setattr(cli.Lake, "users", classmethod(lambda cls: users))
    monkeypatch.setenv("COFFEEDATA_LAKE", main.url)
    cli.main(["delete-user", "maya", "--dry-run"])
    assert "Dry run" in capsys.readouterr().out and "u-maya" in every_value(main)
