"""Core data types shared by every stage of the pipeline."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Platform = Literal["shopify", "woocommerce", "squarespace", "square_online", "generic"]


class Roaster(BaseModel):
    roaster_id: str  # normalized domain; stable and unique
    name: str
    host: str  # hostname that actually serves the site (after redirects)
    platform: str = ""  # from the latest successful probe: shopify, woocommerce, squarespace?, wix?, ...
    city: str = ""
    state: str = ""

    @property
    def adapter(self) -> Platform:
        p = self.platform.rstrip("?")
        if self.platform in ("shopify", "woocommerce"):
            return self.platform  # type: ignore[return-value]
        if p in ("squarespace", "square_online"):
            return p  # type: ignore[return-value]
        return "generic"


class Variant(BaseModel):
    variant_id: str
    title: str = ""
    # The price as the store states it, in `currency` (the name predates the currency field: a store outside the
    # US states its own currency, never converted here). dbt publishes only USD prices.
    price_usd: float | None = None
    # The price's currency as the store states it (an ISO 4217 code: "USD", "ZAR"), as given; None only when the
    # store doesn't state one.
    currency: str | None = None
    grams: int | None = None
    available: bool | None = None


def currency_code(value: object) -> str | None:
    """A store's stated currency, kept as the store gives it (the raw lake stores raw values; dbt reads them as ISO
    4217 codes). None only when the store states none: missing, empty, or not a string."""
    return value if isinstance(value, str) and value.strip() else None


def payload_hash(payload: object) -> str:
    """A stable hash of a raw store payload: canonical JSON (sorted keys), so key order never changes it.
    surrogatepass: a lone surrogate (a truncated emoji escape, which json.loads accepts) hashes instead of failing
    the roaster's whole fetch; every other string encodes as plain UTF-8, so its hash is unchanged."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]


class CatalogItem(BaseModel):
    roaster_id: str
    product_id: str
    handle: str
    url: str
    title: str
    platform: str
    vendor: str = ""
    product_type: str = ""
    tags: list[str] = Field(default_factory=list)
    image: str | None = None
    variants: list[Variant] = Field(default_factory=list)
    body_hash: str = ""
    # When the store last published the product (Shopify only; a republished product gets a new time).
    published_at: datetime | None = None
    # A hash of the store's raw product payload (payload_hash()), so a real change and a repeated fetch can be told
    # apart. Any change the store makes changes it, even one nothing parsed here reads.
    payload_hash: str = ""
    # Body text is used for classification and enrichment but never written to the snapshot.
    body_text: str = Field(default="", exclude=True)

    @field_validator("title")
    @classmethod
    def _unescape_title(cls, v: str) -> str:
        """Some feeds escape titles twice ("Tried &amp;amp; True") or leave markup in them ("3 Month Gift<br>"):
        unescape until nothing changes, then drop tags and extra spaces."""
        for _ in range(3):
            if (u := html.unescape(v)) == v:
                break
            v = u
        return " ".join(re.sub(r"<[^>]{0,40}>", " ", v).split())

    def model_post_init(self, _ctx: object) -> None:
        if self.body_text and not self.body_hash:
            self.body_hash = hashlib.sha1(self.body_text.encode()).hexdigest()[:16]

    @property
    def coffee_id(self) -> str:
        """Stable across runs and renames: the roaster's domain plus the store's own product id."""
        return f"{self.roaster_id}:{self.product_id}"


class Enrichment(BaseModel):
    origin_country: str | None = None
    origin_region: str | None = None
    producer: str | None = None
    process: Literal["washed", "natural", "honey", "anaerobic", "other"] | None = None
    varietals: list[str] = Field(default_factory=list)
    elevation_masl_min: int | None = None
    elevation_masl_max: int | None = None
    roast_level: Literal["light", "medium", "dark"] | None = None
    tasting_notes: list[str] = Field(default_factory=list)
    is_blend: bool | None = None
    is_decaf: bool | None = None
    method: Literal["rules", "llm"] = "rules"
    body_hash: str = ""


Taste = Literal["sour", "balanced", "bitter"]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SAFE_ID = _ID  # ids that are safe in a lake file name


def _safe_id(v: str) -> str:
    # Used in a lake file name: anything but a plain id could escape its table (e.g. "../../x").
    if not _ID.fullmatch(v):
        raise ValueError("ids must be 1-64 letters, digits, '-' or '_'")
    return v


class Shot(BaseModel):
    shot_id: str
    brewed_at: datetime
    coffee_id: str | None = None
    coffee_freetext: str | None = None
    roast_date: date | None = None
    # Checked against the grinder's own dial where the grinder is known (the app); this only bounds nonsense.
    grinder_setting: float = Field(ge=-1000, le=10000)
    dose_g: float = Field(ge=5, le=30)
    yield_g: float = Field(ge=5, le=500)
    time_s: float = Field(ge=1, le=600)
    water_temp_c: float | None = Field(default=None, ge=70, le=100)
    brew_method: str = "espresso"
    rating: int = Field(ge=1, le=5)
    taste: Taste | None = None
    is_milk_drink: bool = False
    notes: str | None = Field(default=None, max_length=2000)
    # Added with the app; all optional, so every older shot stays valid. user_id None means the owner.
    user_id: str | None = None
    grinder_id: str | None = None
    machine_id: str | None = None
    basket_id: str | None = None
    bag_id: str | None = None
    source: Literal["app", "mcp", "issue"] | None = None

    model_config = {"extra": "forbid"}

    _id = field_validator("shot_id")(_safe_id)

    @field_validator("taste", mode="before")
    @classmethod
    def _taste(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower() or None
        return v


class Equipment(BaseModel):
    """A grinder, machine or basket. Changes are new records; the latest per equipment_id wins."""

    equipment_id: str
    user_id: str | None = None
    kind: Literal["grinder", "machine", "basket"]
    name: str = Field(min_length=1, max_length=80)
    model: str | None = None
    scale_min: float | None = None
    scale_max: float | None = None
    scale_step: float | None = Field(default=None, gt=0)
    finer_is_lower: bool | None = None
    basket_dose_g: float | None = Field(default=None, ge=5, le=30)
    is_default: bool = False
    retired: bool = False
    notes: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}

    _id = field_validator("equipment_id")(_safe_id)

    def model_post_init(self, _ctx: object) -> None:
        if self.kind == "grinder":
            if self.scale_min is None or self.scale_max is None or self.scale_step is None:
                raise ValueError("a grinder needs scale_min, scale_max and scale_step")
            if self.scale_min >= self.scale_max:
                raise ValueError("scale_min must be below scale_max")
            if self.finer_is_lower is None:
                self.finer_is_lower = True
        if self.kind == "basket" and self.basket_dose_g is None:
            raise ValueError("a basket needs basket_dose_g")


class Bag(BaseModel):
    """A bag of coffee being used. Finishing or correcting one is a new record with the same bag_id."""

    bag_id: str
    user_id: str | None = None
    coffee_id: str | None = None
    coffee_freetext: str | None = Field(default=None, max_length=200)
    roast_date: date | None = None
    opened_on: date | None = None
    finished_on: date | None = None
    weight_g: int | None = None
    photo_id: str | None = None
    read_json: str | None = None
    match_score: int | None = None

    model_config = {"extra": "forbid"}

    _id = field_validator("bag_id")(_safe_id)


# The "Your next bag" recommendation test: a coffee-level rating, separate from shots, and answers to the picks.
Interest = Literal["want", "maybe", "no"]  # Want to try / Maybe / Not for me
RatingContext = Literal["seed", "picks", "later"]  # the five that seed the picks; from the picks; rated later
DrankAs = Literal["espresso", "filter", "milk"]
# A catalog id ("roaster:product"): checked against the catalog or the person's picks, so this only bounds it.
_COFFEE_ID = Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f]+$")


class Rating(BaseModel):
    """One person's 1-5 rating of one catalog coffee. A correction is a new record under the same rating_id and
    an undo is one with deleted = true: the latest record per rating_id wins, and none is ever changed."""

    rating_id: str
    user_id: str | None = None
    coffee_id: str = _COFFEE_ID
    rating: int = Field(ge=1, le=5)
    drank_as: DrankAs | None = None
    context: RatingContext = "seed"

    model_config = {"extra": "forbid"}

    _id = field_validator("rating_id")(_safe_id)


class RecResponse(BaseModel):
    """One person's answer to one coffee of their slate: how it sounds, and whether it's the one they'd buy next.
    A changed answer is a new record; the latest per (user_id, slate_id, coffee_id) wins."""

    user_id: str | None = None
    slate_id: str
    coffee_id: str = _COFFEE_ID
    interest: Interest | None = None  # None: only the pick is answered so far
    picked: bool = False

    model_config = {"extra": "forbid"}

    _id = field_validator("slate_id")(_safe_id)


_GRAMS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:kg|kilo)", re.I), 1000.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|#)", re.I), 453.592),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce)", re.I), 28.3495),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:g|gr|gram)s?\b", re.I), 1.0),
]


def parse_grams(text: str) -> int | None:
    """'12 oz' -> 340, '2 lb' -> 907, '250g' -> 250. None when no weight is present."""
    for pat, factor in _GRAMS:
        m = pat.search(text or "")
        if m:
            grams = round(float(m.group(1)) * factor)
            return grams if 30 <= grams <= 25_000 else None
    return None
