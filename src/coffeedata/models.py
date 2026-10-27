"""Core data types shared by every stage of the pipeline."""

from __future__ import annotations

import hashlib
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
    is_chain: bool = False

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
    price_usd: float | None = None
    grams: int | None = None
    available: bool | None = None


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
    # Body text is used for classification and enrichment but never written to the snapshot.
    body_text: str = Field(default="", exclude=True)

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


class Shot(BaseModel):
    shot_id: str
    brewed_at: datetime
    coffee_id: str | None = None
    coffee_freetext: str | None = None
    roast_date: date | None = None
    grinder_setting: float = Field(ge=0, le=100)
    dose_g: float = Field(ge=5, le=30)
    yield_g: float = Field(ge=5, le=500)
    time_s: float = Field(ge=1, le=600)
    water_temp_c: float | None = Field(default=None, ge=70, le=100)
    brew_method: str = "espresso"
    rating: int = Field(ge=1, le=5)
    taste: Taste | None = None
    is_milk_drink: bool = False
    notes: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}

    @field_validator("shot_id")
    @classmethod
    def _safe_id(cls, v: str) -> str:
        # Used in a lake file name: anything but a plain id could escape shots/ (e.g. "../../x").
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", v):
            raise ValueError("shot_id must be 1-64 letters, digits, '-' or '_'")
        return v

    @field_validator("taste", mode="before")
    @classmethod
    def _taste(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower() or None
        return v


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
