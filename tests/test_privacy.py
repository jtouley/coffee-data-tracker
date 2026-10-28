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


def test_the_app_uses_exactly_the_four_cookies_the_policy_lists(app, owner):  # noqa: F811
    # The server sets the session cookie; the page script sets theme, hand and tz.
    names = set()
    c = client(app)
    for r in (
        c.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=W),
        c.get("/"),
        c.get("/setup"),
        c.post("/api/signout", json={}, headers=W),
    ):
        names |= {h.split("=", 1)[0] for h in r.headers.get_list("set-cookie")}
    js = (ROOT / "app/static/app.js").read_text()
    assert js.count("document.cookie =") == 2  # tz, and the one in preference()
    names |= set(re.findall(r"document\.cookie = `(\w+)=", js))
    names |= set(re.findall(r'preference\(\w+, "(\w+)"', js))
    assert names == {"session", "theme", "hand", "tz"}
    policy = (ROOT / "src/coffeedata/templates/privacy.html").read_text()
    assert set(re.findall(r"<li><code>(\w+)</code>", policy)) == names


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
    later = datetime(2026, 10, 21, 7, tzinfo=UTC)
    for uid, name, role in (("owner", "jtouley", "owner"), ("u-maya", "maya", "member")):
        users.write("users", f"users/{uid}__20261020T070000.parquet",
                    [{"user_id": uid, "username": name, "role": role, "logged_at": t}])  # fmt: skip
        shot = {"shot_id": f"s-{uid}", "user_id": uid, "logged_at": t, "brewed_at": t, "dose_g": 18.0,
                "yield_g": 36.0, "time_s": 28.0, "grinder_setting": 11.0, "rating": 4}  # fmt: skip
        main.write("shots", f"shots/s-{uid}__20261020T070000.parquet", [shot])
        main.write(
            "bags",
            f"bags/b-{uid}__20261020T070000.parquet",
            [{"bag_id": f"b-{uid}", "user_id": uid, "logged_at": t}],
        )
        photos.write("photos", f"photos/p-{uid}.parquet", [{"photo_id": f"p-{uid}", "user_id": uid}])
    # Maya's account is turned off in the app before her data is deleted.
    users.write("users", "users/u-maya__20261021T070000.parquet",
                [{"user_id": "u-maya", "username": "maya", "role": "member", "disabled": True, "logged_at": later}])  # fmt: skip
    # Her invite: first unused, then used by her. Both versions must go, or it would work again (#24 review 1).
    users.write("invites", "invites/i1__20261020T070000.parquet",
                [{"invite_id": "i1", "created_by": "owner", "kind": "join", "logged_at": t,
                  "expires_at": datetime(2099, 1, 1, tzinfo=UTC)}])  # fmt: skip
    users.write("invites", "invites/i1__20261021T070000.parquet",
                [{"invite_id": "i1", "created_by": "owner", "kind": "join", "used_by": "u-maya", "logged_at": later,
                  "expires_at": datetime(2099, 1, 1, tzinfo=UTC)}])  # fmt: skip
    # The weekly shot export the app seeds from, built before the deletion (#24 review 3).
    con = main.connect()
    main.write_mart(
        "fct_shot",
        con,
        f"""select * from read_parquet('{main.root}/shots/*.parquet', union_by_name = true)
        cross join (select null::varchar shot_coffee_id, null::varchar shot_coffee_freetext, null::date shot_roast_date)""",
    )
    return main, photos, users


def test_delete_user_removes_every_version_of_their_records_and_nobody_elses(three):
    main, photos, users = three
    p = erase.plan("Maya", main, photos, users)
    assert p.user_id == "u-maya" and p.marts == ["fct_shot"]
    assert p.files == {
        main.url: ["bags/b-u-maya__20261020T070000.parquet", "shots/s-u-maya__20261020T070000.parquet"],
        photos.url: ["photos/p-u-maya.parquet"],
        users.url: ["invites/i1__20261020T070000.parquet", "invites/i1__20261021T070000.parquet",
                    "users/u-maya__20261020T070000.parquet", "users/u-maya__20261021T070000.parquet"],
    }  # fmt: skip
    erase.apply(p, main, photos, users)
    left = every_value(main) + every_value(photos) + every_value(users)
    assert "u-maya" not in left and "s-owner" in left and "p-owner" in left
    assert not any(
        i["invite_id"] == "i1" for i in users.latest("invites", "invite_id")
    )  # the invite can't come back


def test_the_app_doesnt_load_a_deleted_persons_shots_back_from_the_export(three):
    from app.store import Store

    main, photos, users = three
    erase.apply(erase.plan("maya", main, photos, users), main, photos, users)
    assert set(Store(main, "owner").shots) == {"s-owner"}


def test_delete_user_needs_the_account_turned_off_first(three):
    # A turned-off account can't write (its sessions end, sign-in refuses it), so the running app can't recreate
    # the files being deleted (#24 review 4).
    main, photos, users = three
    users.write("users", "users/u-maya__20261022T070000.parquet",
                [{"user_id": "u-maya", "username": "maya", "role": "member", "disabled": False,
                  "logged_at": datetime(2026, 10, 22, 7, tzinfo=UTC)}])  # fmt: skip
    with pytest.raises(erase.EraseError, match="Turn maya's account off"):
        erase.plan("maya", main, photos, users)
    assert erase.plan("maya", main, photos, users, force=True).user_id == "u-maya"


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
    out = capsys.readouterr().out
    assert "Dry run" in out and "marts/fct_shot.parquet" in out and "u-maya" in every_value(main)
