"""Each user's shots, bags and equipment, in memory: loaded from the lake at start and updated on every write.

Every write is one new lake file through `Lake.write`, published with `Lake.publish` before it is acknowledged.
Ids come from the phone, so a retried request carries the same id: an identical copy of what's already stored is
acknowledged without writing; anything else is a correction (a new record, latest wins).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from coffeedata import shots
from coffeedata.lake import Lake
from coffeedata.models import Bag, Equipment, Shot

log = logging.getLogger(__name__)


# What the screens show about a catalog coffee (whatever of it this week's export has).
FACTS = ("title", "roaster_name", "roaster_state", "origin_country", "origin_region", "process", "tasting_notes", "url",
         "elevation_masl_min", "elevation_masl_max",
         "price_per_100g", "min_price_usd", "bag_sizes_g", "in_stock")  # fmt: skip
MISS_TTL = 3600  # seconds before a coffee missing from the catalog export is looked up again


class NotFound(KeyError):
    """Missing, or someone else's: the two look the same from outside."""


class Invalid(ValueError):
    """A request that can't be applied, with a message to show."""


@dataclass
class Rec:
    item: Any  # Shot, Bag or Equipment
    logged_at: datetime
    deleted: bool = False


def _model[M: BaseModel](cls: type[M], row: dict) -> M:
    return cls(**{k: v for k, v in row.items() if k in cls.model_fields and v is not None})


def coffee_key(x: Shot | Bag) -> str:
    """Same grouping as mart_dial_in: the catalog coffee, else the typed name."""
    return x.coffee_id or f"freetext:{(x.coffee_freetext or '').strip().lower()}"


class Store:
    def __init__(self, lake: Lake, owner_id: str):
        self.lake, self.owner_id = lake, owner_id
        self.lock = threading.Lock()
        self.shots: dict[str, Rec] = {}
        self.bags: dict[str, Rec] = {}
        self.equipment: dict[str, Rec] = {}
        self._coffees: dict[str, tuple[dict | None, float]] = {}
        self._staged: dict[str, datetime] = {}
        for table, key, cls, into in (
            ("shots", "shot_id", Shot, self.shots),
            ("bags", "bag_id", Bag, self.bags),
            ("equipment", "equipment_id", Equipment, self.equipment),
        ):
            rows = self._seed_shots() if table == "shots" else None
            for row in lake.latest(table, key) if rows is None else rows:
                row["user_id"] = row["user_id"] or owner_id  # records from before accounts are the owner's
                try:
                    into[row[key]] = Rec(_model(cls, row), row["logged_at"], bool(row.get("deleted")))
                except ValidationError as e:
                    log.warning("skipping %s %s: %s", table, row[key], e)

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
        cutoff = max((r["logged_at"] for r in rows.values()), default=None)
        newer = self.lake.files_after("shots", cutoff) if cutoff else None
        if newer is None:
            return None
        if newer:
            files = ", ".join(f"'{f}'" for f in newer)
            cur = con.execute(f"""
                select * from read_parquet([{files}], union_by_name = true, hive_partitioning = false)
                qualify row_number() over (partition by shot_id order by logged_at desc) = 1""")
            cols = [d[0] for d in cur.description]
            for r in (dict(zip(cols, x, strict=True)) for x in cur.fetchall()):
                rows[r["shot_id"]] = r  # newer than anything in the export: latest wins
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
        return r.item if r and r.item.user_id == user_id else None

    def coffee(self, coffee_id: str | None) -> dict | None:
        """Catalog facts for a coffee from the weekly dim_coffee export, if there is one."""
        if not coffee_id:
            return None
        hit = self._coffees.get(coffee_id)
        if hit and (hit[0] is not None or time.monotonic() - hit[1] < MISS_TTL):
            return hit[0]
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
            return None  # not cached: try again next time
        # A coffee not in this week's export is asked again after MISS_TTL, not never.
        self._coffees[coffee_id] = (found, time.monotonic())
        return found

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
        """Highest rating; the latest wins ties (as in mart_dial_in and mart_bag)."""
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
    ) -> dict:
        """What the Log shot form starts from: the last shot of the current bag, else of the same coffee.
        Time, taste and rating are never copied; the last ones are offered, not filled in. A grinder or basket
        picked from the chip replaces the last shot's; the grind then comes from this coffee's last shot on
        that grinder, since a setting means nothing on another grinder."""
        bag = self.current_bag(user_id)
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

    def _commit(self, recs: dict[str, Rec], key: str, item: Any, table: str, deleted: bool = False) -> Rec:
        """One lake file for one record, published before returning; the cache changes only after that.

        The lock covers staging the file, which fixes its logged_at, not the upload, so a slow commit to Hugging
        Face doesn't hold up everyone else's writes. Lake.publish runs one upload at a time and sends whatever is
        staged, so a request's file may go up in another request's commit."""
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
        self.lake.publish(f"app: {table} {key}")
        rec = Rec(item, at, deleted)
        with self.lock:
            current = recs.get(key)
            if current is None or current.logged_at <= at:  # a later record may have landed meanwhile
                recs[key] = rec
        return rec

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
        if bag and not shot.coffee_id and not shot.coffee_freetext:  # the bag says which coffee it is
            shot = shot.model_copy(
                update={"coffee_id": bag.coffee_id, "coffee_freetext": bag.coffee_freetext}
            )
        prev = self._owned(self.shots, shot.shot_id, user_id)
        if prev and prev.item == shot:
            # A retry of a write that already landed, including one that arrives after Undo: a late resend
            # must not bring a retracted shot back.
            return shot, False
        self._commit(self.shots, shot.shot_id, shot, "shots")
        return shot, True

    def retract_shot(self, user_id: str, shot_id: str) -> bool:
        """Undo: a retraction record (deleted = true), never a delete. Retracting twice writes once."""
        prev = self._owned(self.shots, shot_id, user_id)
        if prev is None:
            raise NotFound(shot_id)
        if prev.deleted:
            return False
        self._commit(self.shots, shot_id, prev.item, "shots", deleted=True)
        return True

    def restore_shot(self, user_id: str, shot_id: str) -> bool:
        """Undo a retraction: the shot again, as a new record. Restoring a shot that's there writes nothing."""
        prev = self._owned(self.shots, shot_id, user_id)
        if prev is None:
            raise NotFound(shot_id)
        if not prev.deleted:
            return False
        self._commit(self.shots, shot_id, prev.item, "shots")
        return True

    def save_equipment(self, user_id: str, data: dict) -> Equipment:
        """Add or change equipment. One default per kind: making this one the default clears the others."""
        item = Equipment(**{k: v for k, v in data.items() if k != "user_id"}, user_id=user_id)
        prev = self._owned(self.equipment, item.equipment_id, user_id)
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
