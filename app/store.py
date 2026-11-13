"""Each user's shots, bags and equipment, in memory: loaded from the lake at start and updated on every write.

Every write is one new lake file through `Lake.write`, published with `Lake.publish` before it is acknowledged.
Ids come from the phone, so a retried request carries the same id: an identical copy of what's already stored is
acknowledged without writing; anything else is a correction (a new record, latest wins).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from coffeedata import shots
from coffeedata.lake import Lake, no_files
from coffeedata.models import Bag, Equipment, Rating, RecResponse, Shot

from . import logs

log = logging.getLogger(__name__)


# What the screens show about a catalog coffee (whatever of it this week's export has).
FACTS = ("title", "roaster_name", "roaster_state", "origin_country", "origin_region", "process", "tasting_notes", "url",
         "elevation_masl_min", "elevation_masl_max",
         "price_per_100g", "min_price_usd", "bag_sizes_g", "in_stock", "catalog_status")  # fmt: skip
MISS_TTL = 3600  # seconds before a coffee missing from the catalog export is looked up again
# Tries for each table read at start: a Hugging Face hiccup (429, timeout) waits 2 then 4 seconds rather than
# crashing the only instance, which Cloud Run would answer with "no available instance" until another starts.
STARTUP_READS = 3
SEED_SLACK = timedelta(days=1)  # how far before the shot export the newer shot files are read from
SLATE_TTL = 600  # seconds the picks export (marts/mart_rec_slate.parquet) is kept before it's read again
SLATE_RECHECK = 60  # an answer for a coffee not in the copy held reads it again, at most this often (seconds)


class NotFound(KeyError):
    """Missing, or someone else's: the two look the same from outside."""


class Invalid(ValueError):
    """A request that can't be applied, with a message to show."""


class Unavailable(RuntimeError):
    """Something a write is checked against (the catalog, the picks) couldn't be read: try again later."""


@dataclass
class Rec:
    item: Any  # Shot, Bag or Equipment
    logged_at: datetime
    deleted: bool = False


@dataclass
class _Pending:
    item: Any
    deleted: bool
    ok: bool = False
    done: threading.Event = field(default_factory=threading.Event)


def _model[M: BaseModel](cls: type[M], row: dict) -> M:
    return cls(**{k: v for k, v in row.items() if k in cls.model_fields and v is not None})


def coffee_key(x: Shot | Bag) -> str:
    """Same grouping as mart_dial_in: the catalog coffee, else the typed name."""
    return x.coffee_id or f"freetext:{(x.coffee_freetext or '').strip().lower()}"


def response_key(user_id: str, slate_id: str, coffee_id: str) -> str:
    """One answer's key, also its file name stem: a catalog id ("roaster.com:123") isn't safe in a file name, so
    the coffee is named by a hash of its id. The record itself carries the coffee_id."""
    return f"{user_id}__{slate_id}__{hashlib.sha256(coffee_id.encode()).hexdigest()[:16]}"


class Store:
    def __init__(self, lake: Lake, owner_id: str):
        self.lake, self.owner_id = lake, owner_id
        self.lock = threading.Lock()
        self.shots: dict[str, Rec] = {}
        self.bags: dict[str, Rec] = {}
        self.equipment: dict[str, Rec] = {}
        self.ratings: dict[str, Rec] = {}
        self.responses: dict[str, Rec] = {}  # response_key -> the latest answer
        self._slates: tuple[dict[str, set[tuple[str, str]]], float] | None = None  # user -> {(slate, coffee)}
        self._slate_lock = threading.Lock()  # one read of the picks export at a time
        self._slate_failed: float | None = None  # when the last read of it failed, to back off
        # One rating or answer write at a time per person: each reads what's stored, checks it and writes, and two
        # at once could both pass a check or merge into the same old answer (review B1).
        self._user_locks: dict[str, threading.Lock] = {}
        self._coffees: dict[str, tuple[dict | None, float]] = {}
        self._staged: dict[str, datetime] = {}
        self._pending: dict[tuple[str, str], _Pending] = {}  # the latest record per key still being published
        # (table, key, model, into, whether a record with no user is the owner's: those from before accounts are;
        # the recommendation test's records always name their user)
        for table, key, cls, into, owners in (
            ("shots", "shot_id", Shot, self.shots, True),
            ("bags", "bag_id", Bag, self.bags, True),
            ("equipment", "equipment_id", Equipment, self.equipment, True),
            ("ratings", "rating_id", Rating, self.ratings, False),
        ):
            rows = self._seed_shots() if table == "shots" else None
            for row in lake.latest(table, key, attempts=STARTUP_READS) if rows is None else rows:
                if owners:
                    row["user_id"] = row["user_id"] or owner_id
                try:
                    into[row[key]] = Rec(_model(cls, row), row["logged_at"], bool(row.get("deleted")))
                except ValidationError as e:
                    log.warning("skipping %s %s: %s", table, row[key], e)
        for row in lake.latest("rec_responses", ("user_id", "slate_id", "coffee_id"), attempts=STARTUP_READS):
            try:
                item = _model(RecResponse, row)
            except ValidationError as e:
                log.warning("skipping rec_responses %s: %s", row["slate_id"], e)
                continue
            key = response_key(row["user_id"], item.slate_id, item.coffee_id)
            self.responses[key] = Rec(item, row["logged_at"])

    def _seed_shots(self) -> list[dict] | None:
        """Current shots from the weekly marts/fct_shot.parquet export plus the shot files written after it,
        instead of one request per shot file. None (read every file) when there's no export yet."""
        con = self.lake.connect()
        try:
            src = ", ".join(f"'{s}'" for s in self.lake.mart_sources("fct_shot"))
            cur = con.execute(f"select * from read_parquet([{src}])")
        except Exception as e:  # noqa: BLE001 - no export yet, or one from before logged_at was in it
            log.info("no fct_shot export to start from: %s", e)
            return None
        cols = [d[0] for d in cur.description]
        if "logged_at" not in cols:
            return None
        if "shot_coffee_id" not in cols:
            return None  # an export from before it kept the shot's own values
        rows = {}
        for x in cur.fetchall():
            r = dict(zip(cols, x, strict=True))
            for c in ("coffee_id", "coffee_freetext", "roast_date"):
                r[c] = r.pop(f"shot_{c}")  # the raw shot, not the bag's values fct_shot fills in
            rows[r["shot_id"]] = r
        # File names carry the time a record was made, not when it reached the dataset, and uploads queue. So
        # the files are read from a day before the export (its snapshot_at, else its newest record): a record made
        # before the export but uploaded after it is still found. Overlap with the export is harmless: latest wins.
        if "snapshot_at" in cols:
            stamp = max((r["snapshot_at"] for r in rows.values() if r.get("snapshot_at")), default=None)
        else:
            stamp = max((r["logged_at"] for r in rows.values()), default=None)
        if stamp is None:
            return None
        try:
            newer = self.lake.files_after("shots", stamp - SEED_SLACK)
            if newer:
                files = ", ".join(f"'{f}'" for f in newer)
                cur = con.execute(f"""
                    select * from read_parquet([{files}], union_by_name = true, hive_partitioning = false)
                    qualify row_number() over (partition by shot_id order by logged_at desc) = 1""")
                cols = [d[0] for d in cur.description]
                for r in (dict(zip(cols, x, strict=True)) for x in cur.fetchall()):
                    if r["shot_id"] not in rows or r["logged_at"] >= rows[r["shot_id"]]["logged_at"]:
                        rows[r["shot_id"]] = r
        except Exception as e:  # noqa: BLE001 - a failed listing (429, timeout): read every file instead
            log.warning("listing newer shot files failed (%s): reading every file", type(e).__name__)
            return None
        log.info("shots: %d from fct_shot, %d newer files", len(rows), len(newer))
        return list(rows.values())

    # --- reads ---------------------------------------------------------------------------------------

    def _mine(self, recs: dict[str, Rec], user_id: str) -> list[Rec]:
        # list() copies the values in one step, so a write from another request thread can't change the dict
        # while this iterates.
        return [r for r in list(recs.values()) if r.item.user_id == user_id and not r.deleted]

    def shots_of(self, user_id: str) -> list[Shot]:
        return sorted((r.item for r in self._mine(self.shots, user_id)), key=lambda s: s.brewed_at)

    def shot(self, user_id: str, shot_id: str) -> Shot:
        r = self.shots.get(shot_id)
        if not r or r.item.user_id != user_id or r.deleted:
            raise NotFound(shot_id)
        return r.item

    def equipment_of(self, user_id: str, kind: str) -> list[Equipment]:
        """Current equipment of a kind, default first, then newest."""
        recs = [r for r in self._mine(self.equipment, user_id) if r.item.kind == kind and not r.item.retired]
        return [r.item for r in sorted(recs, key=lambda r: (r.item.is_default, r.logged_at), reverse=True)]

    def gear(self, user_id: str, equipment_id: str | None) -> Equipment | None:
        r = self.equipment.get(equipment_id or "")
        return r.item if r and r.item.user_id == user_id else None

    def current_bag(self, user_id: str) -> Bag | None:
        open_ = [r for r in self._mine(self.bags, user_id) if not r.item.finished_on]
        return sorted(open_, key=lambda r: r.logged_at)[-1].item if open_ else None  # ties: the last written

    def bag(self, user_id: str, bag_id: str | None) -> Bag | None:
        r = self.bags.get(bag_id or "")
        return r.item if r and r.item.user_id == user_id and not r.deleted else None

    def coffee(self, coffee_id: str | None) -> dict | None:
        """Catalog facts for a coffee from the weekly dim_coffee export, if there is one."""
        return self._lookup(coffee_id)[0]

    def _lookup(self, coffee_id: str | None) -> tuple[dict | None, bool]:
        """A coffee's catalog facts, and whether the answer is known: False when the export couldn't be read."""
        if not coffee_id:
            return None, True
        hit = self._coffees.get(coffee_id)
        if hit and (hit[0] is not None or time.monotonic() - hit[1] < MISS_TTL):
            return hit[0], True
        src = ", ".join(f"'{s}'" for s in self.lake.mart_sources("dim_coffee"))
        try:
            cur = self.lake.connect().execute(
                f"select * from read_parquet([{src}], union_by_name = true) where coffee_id = ?",
                [coffee_id],
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            keep = set(FACTS) & set(cols)
            found = {k: v for k, v in zip(cols, row, strict=True) if k in keep} if row else None
        except Exception as e:  # noqa: BLE001 - no catalog export yet, or the network
            log.info("no catalog facts for %s: %s", coffee_id, e)
            return None, False  # not cached: try again next time
        # A coffee not in this week's export is asked again after MISS_TTL, not never.
        self._coffees[coffee_id] = (found, time.monotonic())
        return found, True

    def bags_of(self, user_id: str) -> list[Bag]:
        """This person's bags, the one used most recently first (a bag with no shots by when it was opened)."""
        mine = self.shots_of(user_id)
        last = {s.bag_id: s.brewed_at for s in mine if s.bag_id}
        recs = self._mine(self.bags, user_id)

        def when(r: Rec) -> datetime:
            return last.get(r.item.bag_id) or r.logged_at

        return [r.item for r in sorted(recs, key=when, reverse=True)]

    def coffee_shots(self, user_id: str, bag: Bag) -> list[Shot]:
        """Every shot of this bag's coffee, from any bag, oldest first."""
        key = coffee_key(bag)
        bag_ids = {b.bag_id for b in self.bags_of(user_id) if coffee_key(b) == key}
        return [
            s
            for s in self.shots_of(user_id)
            if s.bag_id in bag_ids or (not s.bag_id and coffee_key(s) == key)
        ]

    @staticmethod
    def best(shots_: list[Shot]) -> Shot | None:
        """Highest rating; the latest wins ties, as in mart_bag. (mart_dial_in's best_* columns take any one of
        the tied shots.)"""
        return max(shots_, key=lambda s: (s.rating, s.brewed_at), default=None)

    def bag_shots(self, user_id: str, bag: Bag | None, last: Shot | None) -> list[Shot]:
        """The shots shown together: this bag's, or with no bag, the last shot's coffee."""
        mine = self.shots_of(user_id)
        if bag:
            return [s for s in mine if s.bag_id == bag.bag_id]
        return [s for s in mine if last and coffee_key(s) == coffee_key(last)]

    def prefill(
        self,
        user_id: str,
        grinder_id: str | None = None,
        basket_id: str | None = None,
        machine_id: str | None = None,
        bag_id: str | None = None,
    ) -> dict:
        """What the Log shot form starts from: the last shot of the current bag, else of the same coffee.
        Time, taste and rating are never copied; the last ones are offered, not filled in. A grinder or basket
        picked from the chip replaces the last shot's; the grind then comes from this coffee's last shot on
        that grinder, since a setting means nothing on another grinder."""
        selected = self.bag(user_id, bag_id)
        bag = selected if selected and not selected.finished_on else self.current_bag(user_id)
        mine = self.shots_of(user_id)
        last = None
        if bag:
            same = [s for s in mine if s.bag_id == bag.bag_id] or [
                s for s in mine if coffee_key(s) == coffee_key(bag)
            ]
            last = same[-1] if same else None
        elif mine:
            last = mine[-1]
        picked = self.gear(user_id, grinder_id)
        grinder = (
            picked
            if picked and picked.kind == "grinder"
            else self.gear(user_id, last.grinder_id if last else None)
        )
        grinders = self.equipment_of(user_id, "grinder")
        if not grinder or grinder.retired:
            grinder = grinders[0] if grinders else None
        baskets = self.equipment_of(user_id, "basket")
        picked = self.gear(user_id, basket_id)
        basket = (
            picked
            if picked and picked.kind == "basket"
            else self.gear(user_id, last.basket_id if last else None)
        )
        if not basket or basket.retired:  # like the grinder: a retired basket isn't offered again
            basket = baskets[0] if baskets else None
        if grinder and last and last.grinder_id and last.grinder_id != grinder.equipment_id:
            # Switched grinders: this coffee's last shot on the new one, if any, gives the grind.
            on = [
                s for s in mine if s.grinder_id == grinder.equipment_id and coffee_key(s) == coffee_key(last)
            ]
            grind_from = on[-1] if on else None
        else:
            grind_from = last
        dose = last.dose_g if last else (basket.basket_dose_g if basket and basket.basket_dose_g else None)
        grind = None
        if grind_from and (
            not grind_from.grinder_id or not grinder or grind_from.grinder_id == grinder.equipment_id
        ):
            grind = grind_from.grinder_setting
        elif grinder and grinder.scale_min is not None and grinder.scale_max is not None:
            # No shot on this grinder yet: the middle of the dial, a starting point to move from, not advice.
            step = grinder.scale_step or 1
            grind = grinder.scale_min + round((grinder.scale_max - grinder.scale_min) / 2 / step) * step
        return {
            "bag": bag,
            "last": last,
            "grinder": grinder,
            "basket": basket,
            "machine": self._machine(user_id, machine_id, last),
            "coffee_id": bag.coffee_id if bag else last.coffee_id if last else None,
            "coffee_freetext": bag.coffee_freetext if bag else last.coffee_freetext if last else None,
            "dose_g": dose,
            "yield_g": last.yield_g if last else (round(dose * 2, 1) if dose else None),
            "grinder_setting": grind,
            "water_temp_c": last.water_temp_c if last else None,
            "brew_method": last.brew_method if last else "espresso",
            "is_milk_drink": bool(last and last.is_milk_drink),
            "last_time_s": last.time_s if last else None,
            "same_as_last": {"taste": last.taste, "rating": last.rating} if last else None,
        }

    def _machine(self, user_id: str, picked: str | None, last: Shot | None) -> Equipment | None:
        """The picked machine, else the last shot's (unless retired), else the default."""
        for eid in (picked, last.machine_id if last else None):
            e = self.gear(user_id, eid)
            if e and e.kind == "machine" and not e.retired:
                return e
        return next(iter(self.equipment_of(user_id, "machine")), None)

    # --- writes --------------------------------------------------------------------------------------

    def _commit(self, recs: dict[str, Rec], key: str, item: Any, table: str, deleted: bool = False) -> bool:
        """One lake file for one record, published before returning; the cache changes only after that. Returns
        whether a record was written: one identical to the latest, stored or still being published, isn't.

        The lock covers staging the file, which fixes its logged_at, not the upload, so a slow commit to Hugging
        Face doesn't hold up everyone else's writes. Lake.publish runs one upload at a time and sends whatever is
        staged, so a request's file may go up in another request's commit. While it uploads, the record is kept as
        pending: the outbox resends a write whose page was left mid-save, and that resend waits for the first
        upload instead of writing the same record again. A failed upload drops the mark, so a retry writes."""
        mark = (table, key)
        while True:
            with self.lock:
                busy = self._pending.get(mark)
                if busy is None:
                    current = recs.get(key)
                    if current and current.item == item and current.deleted == deleted:
                        return False  # already stored
                    break
                if busy.item != item or busy.deleted != deleted:
                    break  # a different record: a correction, written after the one in flight
            busy.done.wait()
            if busy.ok:
                return False  # the same record, now published
            # It failed to publish: look again, and write it if nothing else has.
        with self.lock:
            prev = recs.get(key)
            after = max((t for t in (prev and prev.logged_at, self._staged.get(key)) if t), default=None)
            if table == "shots":
                at = shots.record(item, self.lake, deleted, after=after)
            else:
                at = datetime.now(UTC).replace(microsecond=0)
                if after and at <= after:
                    at = after + timedelta(seconds=1)
                row = item.model_dump() | {"logged_at": at, "deleted": deleted}
                self.lake.write(table, f"{table}/{key}__{at:%Y%m%dT%H%M%S}.parquet", [row])
            self._staged[key] = at  # the next record for this key sorts after this one, published or not
            mine = self._pending[mark] = _Pending(item, deleted)
        started = time.perf_counter()
        try:
            self.lake.publish(f"app: {table} {key}")
        except BaseException:
            with self.lock:
                if self._pending.get(mark) is mine:
                    del self._pending[mark]
            mine.done.set()  # ok stays False: whoever waited writes it themselves
            raise
        logs.event(
            "saved",
            table=table,
            id=key,
            deleted=deleted,
            publish_ms=round((time.perf_counter() - started) * 1000),
        )
        rec = Rec(item, at, deleted)
        with self.lock:
            current = recs.get(key)
            if current is None or current.logged_at <= at:  # a later record may have landed meanwhile
                recs[key] = rec
            if self._pending.get(mark) is mine:
                del self._pending[mark]
            mine.ok = True
        mine.done.set()
        return True

    def _owned(self, recs: dict[str, Rec], key: str, user_id: str) -> Rec | None:
        prev = recs.get(key)
        if prev and prev.item.user_id != user_id:
            raise NotFound(key)
        return prev

    def save_shot(self, user_id: str, data: dict) -> tuple[Shot, bool]:
        """Record a shot from the app. Returns the shot and whether a new record was written."""
        data = {k: v for k, v in data.items() if k not in ("user_id", "source")}
        shot = Shot(**data, user_id=user_id, source="app")
        shot = shot.model_copy(update={"brewed_at": shot.brewed_at.astimezone(UTC).replace(microsecond=0)})
        for ref, table in (
            (shot.grinder_id, self.equipment),
            (shot.basket_id, self.equipment),
            (shot.machine_id, self.equipment),
            (shot.bag_id, self.bags),
        ):
            if ref and self._owned(table, ref, user_id) is None:
                raise NotFound(ref)
        grinder = self.gear(user_id, shot.grinder_id)
        lo, hi = (grinder.scale_min, grinder.scale_max) if grinder else (None, None)
        if lo is not None and hi is not None and not lo <= shot.grinder_setting <= hi:
            raise Invalid(
                f"Grind {shot.grinder_setting:g} is outside {grinder.name if grinder else 'the grinder'}'s dial "
                f"({lo:g} to {hi:g})."
            )
        bag = self.bag(user_id, shot.bag_id)
        if shot.bag_id and bag is None:
            raise NotFound(shot.bag_id)
        prev = self._owned(self.shots, shot.shot_id, user_id)
        moved = prev is not None and bag is not None and prev.item.bag_id != bag.bag_id
        if bag and (moved or (not shot.coffee_id and not shot.coffee_freetext)):
            # The bag says which coffee it is; a correction that moves the shot to another bag takes that bag's.
            shot = shot.model_copy(
                update={"coffee_id": bag.coffee_id, "coffee_freetext": bag.coffee_freetext}
            )
        if prev:
            # A correction keeps what the Log shot form doesn't carry: where the shot came from and its roast date.
            # A shot's own date is one entered with it (an import); without one, fct_shot takes its bag's. Moved, a
            # date that is only the old bag's (copied in by an earlier move) is dropped, so the shot follows its new
            # bag's date, and a later fix to that bag's date, rather than keeping a copy of either.
            roast = shot.roast_date or prev.item.roast_date
            old_bag = self.bag(user_id, prev.item.bag_id)
            if moved and not shot.roast_date and old_bag and roast == old_bag.roast_date:
                roast = None
            shot = shot.model_copy(update={"source": prev.item.source, "roast_date": roast})
        if prev and prev.item == shot:
            # A retry of a write that already landed, including one that arrives after Undo: a late resend
            # must not bring a retracted shot back.
            return shot, False
        if prev and prev.deleted:
            # Only Undo (restore_shot) brings a retracted shot back, not a correction saved in another tab.
            raise Invalid("That shot was retracted, so it can’t be corrected. Undo the retraction first.")
        return shot, self._commit(self.shots, shot.shot_id, shot, "shots")

    def retract_shot(self, user_id: str, shot_id: str) -> bool:
        """Undo: a retraction record (deleted = true), never a delete. Retracting twice writes once."""
        prev = self._owned(self.shots, shot_id, user_id)
        if prev is None:
            raise NotFound(shot_id)
        if prev.deleted:
            return False
        return self._commit(self.shots, shot_id, prev.item, "shots", deleted=True)

    def restore_shot(self, user_id: str, shot_id: str) -> bool:
        """Undo a retraction: the shot again, as a new record. Restoring a shot that's there writes nothing."""
        prev = self._owned(self.shots, shot_id, user_id)
        if prev is None:
            raise NotFound(shot_id)
        if not prev.deleted:
            return False
        return self._commit(self.shots, shot_id, prev.item, "shots")

    def save_equipment(self, user_id: str, data: dict) -> Equipment:
        """Add or change equipment. One default per kind: making this one the default clears the others.
        A change keeps what the body leaves out (notes, a model the form doesn't show, retired), so editing a
        grinder made elsewhere doesn't erase its notes and saving a retired one doesn't bring it back."""
        data = {k: v for k, v in data.items() if k != "user_id"}
        prev = self._owned(self.equipment, str(data.get("equipment_id") or ""), user_id)
        if prev:
            kept = prev.item.model_dump(exclude={"user_id"})
            data = {k: v for k, v in kept.items() if k not in data} | data
        item = Equipment(**data, user_id=user_id)
        if prev and prev.item.kind != item.kind:
            raise Invalid("Equipment can't change kind. Add a new one instead.")
        if item.retired:
            item = item.model_copy(update={"is_default": False})
        if not (prev and prev.item == item):
            self._commit(self.equipment, item.equipment_id, item, "equipment")
        if item.is_default:
            for other in self.equipment_of(user_id, item.kind):
                if other.equipment_id != item.equipment_id and other.is_default:
                    self._commit(
                        self.equipment,
                        other.equipment_id,
                        other.model_copy(update={"is_default": False}),
                        "equipment",
                    )
        return item

    def save_bag(self, user_id: str, data: dict) -> tuple[Bag, list[dict]]:
        """A bag typed by hand: matched to the catalog the same way a typed shot is."""
        bag = Bag(**{k: v for k, v in data.items() if k != "user_id"}, user_id=user_id)
        prev = self._owned(self.bags, bag.bag_id, user_id)
        # Today in UTC plus a day's grace (the phone checks the same), so no zone's "today" is refused. Only a date
        # being set now is refused, so a bag stored before these checks (with a future or old date) can still take its
        # photo's match.
        new_date = not (prev and prev.item.roast_date == bag.roast_date)
        today = datetime.now(UTC).date()
        if new_date and bag.roast_date and bag.roast_date > today + timedelta(days=1):
            raise Invalid("That roast date is in the future. Check the year.")
        # Over a year old (with the same day of grace, as labels.latest_past): the year is almost surely wrong.
        if new_date and bag.roast_date and bag.roast_date < today - timedelta(days=366):
            raise Invalid("That roast date is over a year ago. Check the year.")
        same_text = prev and (prev.item.coffee_freetext or "").strip() == (bag.coffee_freetext or "").strip()
        if prev and same_text and not bag.coffee_id:
            # A resend or an edit of something else keeps the match; changed text is matched again below.
            bag = bag.model_copy(
                update={"coffee_id": prev.item.coffee_id, "match_score": prev.item.match_score}
            )
        matches: list[dict] = []
        if not bag.coffee_id and bag.coffee_freetext:
            coffee_id, matches = shots.match_coffee(bag.coffee_freetext, self.lake)
            score = matches[0]["score"] if coffee_id else None
            bag = bag.model_copy(update={"coffee_id": coffee_id, "match_score": score})
        opened = bag.opened_on or (prev.item.opened_on if prev else None) or date.today()
        bag = bag.model_copy(update={"opened_on": opened})
        if not (prev and prev.item == bag):
            self._commit(self.bags, bag.bag_id, bag, "bags")
        return bag, matches

    # --- the "Your next bag" test: coffee ratings and answers to the picks ----------------------------------------

    def ratings_of(self, user_id: str) -> list[Rating]:
        """This person's current ratings, oldest first."""
        return [r.item for r in sorted(self._mine(self.ratings, user_id), key=lambda r: r.logged_at)]

    def responses_of(self, user_id: str, slate_id: str | None = None) -> list[RecResponse]:
        return [
            r.item
            for r in list(self.responses.values())
            if r.item.user_id == user_id and (slate_id is None or r.item.slate_id == slate_id)
        ]

    def _in_catalog(self, coffee_id: str) -> bool:
        """Whether the catalog export has this coffee. Unavailable when the export couldn't be read, so a phone's
        save is tried again rather than refused for good."""
        found, known = self._lookup(coffee_id)
        if not known:  # the read itself failed, whatever an older read said
            raise Unavailable("The catalog isn't available right now. Try again shortly.")
        return found is not None

    def _read_slates(self) -> dict[str, set[tuple[str, str]]]:
        """Every (slate_id, coffee_id) per person, from the picks export dbt writes (mart_rec_slate: user_id,
        slate_id, position, coffee_id); none before the first export exists. Unavailable if the read fails, and if
        the export is missing while a copy is held (review S7): a missing file then is a failure, not "no picks"."""
        src = ", ".join(f"'{s}'" for s in self.lake.mart_sources("mart_rec_slate"))
        try:
            rows = (
                self.lake.connect()
                .execute(f"select user_id, slate_id, coffee_id from read_parquet([{src}])")
                .fetchall()
            )
        except Exception as e:  # noqa: BLE001 - duckdb's IOException: no export yet, or the network
            if not (no_files(e) or "404" in str(e)) or self._slates is not None:
                log.warning("reading the picks export failed (%s)", type(e).__name__)
                raise Unavailable("Your picks couldn't be read right now. Try again shortly.") from None
            rows = []
        out: dict[str, set[tuple[str, str]]] = {}
        for uid, sid, cid in rows:
            out.setdefault(uid, set()).add((sid, cid))
        return out

    def slate_pairs(self, user_id: str) -> set[tuple[str, str]]:
        """(slate_id, coffee_id) of every coffee this person was suggested, read again after SLATE_TTL."""
        return self._slate_pairs(user_id, SLATE_TTL)[0]

    def _slate_pairs(self, user_id: str, older_than: float) -> tuple[set[tuple[str, str]], bool]:
        """The pairs, and whether they were read just now. The export is read again once the copy held is
        `older_than` seconds old, by one request at a time. If that read fails the copy held is used; only with
        none held is it Unavailable. After a failed read the export isn't tried again for SLATE_RECHECK (S8)."""
        fresh = False
        with self._slate_lock:
            cached, now = self._slates, time.monotonic()
            due = cached is None or now - cached[1] >= older_than
            backing_off = self._slate_failed is not None and now - self._slate_failed < SLATE_RECHECK
            if due and backing_off and cached is None:
                raise Unavailable("Your picks couldn't be read right now. Try again shortly.")
            if due and not backing_off:
                try:
                    cached = self._slates = (self._read_slates(), time.monotonic())
                    self._slate_failed, fresh = None, True
                except Unavailable:
                    self._slate_failed = time.monotonic()
                    if cached is None:
                        raise
        assert cached is not None  # every path above either read it, held it or raised
        return cached[0].get(user_id, set()), fresh

    def _in_slate(self, user_id: str, slate_id: str, coffee_id: str) -> None:
        """Refuse (404) a coffee that isn't one of this person's picks, but only on a read made just now: one the
        copy held lacks may be in an export published since, so the export is read again (at most once a
        SLATE_RECHECK), and while that's too soon the answer is try-again (503), never a refusal of a new slate."""
        pair = (slate_id, coffee_id)
        for older_than in (SLATE_TTL, SLATE_RECHECK):
            pairs, fresh = self._slate_pairs(user_id, older_than)
            if pair in pairs:
                return
            if fresh:
                raise NotFound(coffee_id)
        raise Unavailable("Your picks may have just changed. This is sent again shortly.")

    def forget_recs(self, user_id: str) -> None:
        """Drop a person's ratings and answers from memory (their account was turned off). An erasure
        (delete-user --only-recs) can then never be written back by this process, even if the account is turned on
        again before the restart: an Undo finds nothing, and a new answer has no old one to merge (review B3)."""
        with self._user_lock(user_id), self.lock:
            for recs in (self.ratings, self.responses):
                for key in [k for k, r in recs.items() if r.item.user_id == user_id]:
                    del recs[key]

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self.lock:
            return self._user_locks.setdefault(user_id, threading.Lock())

    def save_rating(self, user_id: str, data: dict) -> tuple[Rating, bool]:
        """A 1-5 rating of a catalog coffee. A correction (the same rating_id) keeps its coffee and context; a
        rating that was undone stays undone. Returns the rating and whether a new record was written."""
        rating = Rating(**{k: v for k, v in data.items() if k != "user_id"}, user_id=user_id)
        with self._user_lock(user_id):
            prev = self._owned(self.ratings, rating.rating_id, user_id)
            if prev:
                if prev.item.coffee_id != rating.coffee_id:
                    raise Invalid("A rating can't move to another coffee. Rate that coffee instead.")
                rating = rating.model_copy(update={"context": prev.item.context})
                if prev.item == rating:
                    return rating, False  # a resend, including one that arrives after Undo
                if prev.deleted:
                    raise Invalid("That rating was undone, so it can't be changed. Rate the coffee again.")
            else:
                if any(
                    r.coffee_id == rating.coffee_id and r.context == rating.context
                    for r in self.ratings_of(user_id)
                ):
                    raise Invalid("You've rated this coffee already. Change that rating instead.")
                if not self._in_catalog(rating.coffee_id):
                    raise Invalid("That coffee isn't in the catalog, so it can't be rated here.")
            return rating, self._commit(self.ratings, rating.rating_id, rating, "ratings")

    def retract_rating(self, user_id: str, rating_id: str) -> bool:
        """Undo: a retraction record (deleted = true), never a delete. Retracting twice writes once."""
        with self._user_lock(user_id):
            prev = self._owned(self.ratings, rating_id, user_id)
            if prev is None:
                raise NotFound(rating_id)
            if prev.deleted:
                return False
            return self._commit(self.ratings, rating_id, prev.item, "ratings", deleted=True)

    def save_rec_response(self, user_id: str, data: dict) -> tuple[RecResponse, bool]:
        """An answer to one suggested coffee: interest, the pick, or both. What the body leaves out is kept from
        the last answer. Picking a coffee un-picks the slate's other one, with a new record for it."""
        data = {k: v for k, v in data.items() if k != "user_id"}
        item = RecResponse(**data, user_id=user_id)
        self._in_slate(user_id, item.slate_id, item.coffee_id)
        key = response_key(user_id, item.slate_id, item.coffee_id)
        with self._user_lock(user_id):
            prev = self.responses.get(key)
            if prev:
                kept = prev.item.model_dump(exclude={"user_id"})
                item = RecResponse(
                    **({k: v for k, v in kept.items() if k not in data} | data), user_id=user_id
                )
            if item.interest is None and not item.picked:
                raise Invalid("Choose how this coffee sounds, or pick it.")
            written = self._commit(self.responses, key, item, "rec_responses")
            if item.picked:
                for other in self.responses_of(user_id, item.slate_id):
                    if other.picked and other.coffee_id != item.coffee_id:
                        unpick = other.model_copy(update={"picked": False})
                        okey = response_key(user_id, other.slate_id, other.coffee_id)
                        self._commit(self.responses, okey, unpick, "rec_responses")
            return item, written
