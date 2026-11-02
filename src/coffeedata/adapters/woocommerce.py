"""WooCommerce stores expose the public Store API at /wp-json/wc/store/v1/products."""

from __future__ import annotations

from collections.abc import Iterator

from ..http import DeadlineReached, FetchError, PartialCatalog, PoliteClient, block_status, challenged
from ..models import CatalogItem, Roaster, Variant, parse_grams, payload_hash
from ..text import html_to_text

# No cap on pages or on variation prices: the whole catalog is read, or the fetch fails, or (at the run's deadline)
# it is stored as partial. The owner removed every cap on 2026-11-02: MAX_VARIATION_LOOKUPS = 150 left 48 of 201
# stores' sizes unpriced, and MAX_PAGES = 20 cut villageofartisans.com at 2,000 products.
PER_PAGE = 100  # the most the Store API returns per page


def _price(prices: dict | None) -> float | None:
    prices = prices or {}
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    return int(raw) / 10 ** int(prices.get("currency_minor_unit", 2))


def _size_label(attrs: list[dict]) -> str:
    return " ".join(str(a.get("value") or "") for a in attrs)


def parse_product(
    p: dict, roaster: Roaster, variation_prices: dict[str, float | None] | None = None
) -> CatalogItem:
    """variation_prices: variation id -> price, fetched separately because the listing only carries a range."""
    tags = sorted(
        {t["name"] for t in p.get("tags", []) if isinstance(t, dict)}
        | {c["name"] for c in p.get("categories", []) if isinstance(c, dict)}
    )
    variations = [v for v in p.get("variations") or [] if isinstance(v, dict)]
    stock = p.get("is_in_stock")
    if variations:
        variants = []
        for v in variations:
            label = _size_label(v.get("attributes") or [])
            price = (variation_prices or {}).get(str(v["id"]))
            variants.append(
                Variant(
                    variant_id=str(v["id"]),
                    title=label,
                    price_usd=price,
                    grams=parse_grams(label),
                    available=stock,
                )
            )
    else:
        name = html_to_text(p.get("name"))
        variants = [
            Variant(
                variant_id=str(p["id"]),
                title=name,
                price_usd=_price(p.get("prices")),
                grams=parse_grams(name),
                available=stock,
            )
        ]
    images = [i for i in p.get("images") or [] if isinstance(i, dict)]
    body = "\n".join(
        filter(None, [html_to_text(p.get("short_description")), html_to_text(p.get("description"))])
    )
    return CatalogItem(
        roaster_id=roaster.roaster_id,
        product_id=str(p["id"]),
        handle=p.get("slug") or str(p["id"]),
        url=p.get("permalink") or f"https://{roaster.host}/?p={p['id']}",
        title=html_to_text(p.get("name")),
        platform="woocommerce",
        product_type=p.get("type") or "",
        tags=tags,
        image=images[0].get("src") if images else None,
        variants=variants,
        body_text=body,
        # The listing payload only: variation_prices depend on how the fetch went (a failed lookup), so hashing them
        # would change the hash of an unchanged product on a repeated fetch.
        payload_hash=payload_hash(p),
    )


def _needs_variation_prices(p: dict) -> bool:
    rng = (p.get("prices") or {}).get("price_range") or {}
    return bool(p.get("variations")) and rng.get("min_amount") != rng.get("max_amount")


def _variation_ids(p: dict) -> list[str]:
    return [str(v["id"]) for v in p.get("variations") or [] if isinstance(v, dict) and "id" in v]


def _pages(client: PoliteClient, url: str, host: str, **params: str | int) -> Iterator[list[dict]]:
    """Every page of a Store API products query, until a short page. A store that ignores `page` and repeats a page
    ends it too (nothing new would come)."""
    seen: set[str] = set()
    page = 1
    while True:
        data = client.fetch_json(url, per_page=PER_PAGE, page=page, **params)
        if not isinstance(data, list):
            raise FetchError(f"unexpected Store API payload from {host}")
        ids = {str(p.get("id")) for p in data if isinstance(p, dict)}
        if data and ids <= seen:
            return
        seen |= ids
        yield data
        if len(data) < PER_PAGE:
            return
        page += 1


def _batches(products: list[dict]) -> Iterator[list[dict]]:
    """Parents grouped so that their listed variations fill about one page each."""
    batch: list[dict] = []
    size = 0
    for p in products:
        n = len(_variation_ids(p))
        if batch and size + n > PER_PAGE:
            yield batch
            batch, size = [], 0
        batch.append(p)
        size += n
    if batch:
        yield batch


def variation_prices(
    client: PoliteClient, base: str, host: str, products: list[dict], prices: dict[str, float | None]
) -> None:
    """Fills `prices` (variation id -> price) for every variation of `products`, in bulk: the Store API's products
    endpoint lists variations with their prices when asked for type=variation, filtered to their parents with
    `parent` (docs/apis/store-api/resources-endpoints/products.md, "Product Variations" and the `parent` and `type`
    parameters; ProductQuery::prepare_objects_query maps type=variation to post_type product_variation and `parent`
    to post_parent__in). About one request per 100 variations, instead of one per variation. A failed page fails
    the fetch (a listing page's failure would). A variation the bulk query didn't return (a store that ignores
    type=variation, or an older Store API) is asked for on its own, as before, with no cap; only that lookup's
    failure leaves its price unknown."""
    wanted = {vid for p in products for vid in _variation_ids(p)}
    for batch in _batches(products):
        mine = {vid for p in batch for vid in _variation_ids(p)}
        parents = ",".join(str(p["id"]) for p in batch)
        foreign = False
        for data in _pages(client, base, host, type="variation", parent=parents):
            for v in data:
                if isinstance(v, dict) and "id" in v:
                    vid = str(v["id"])
                    foreign |= vid not in mine
                    if vid in wanted:
                        prices[vid] = _price(v.get("prices"))
            if foreign or mine <= prices.keys():  # every listed size priced: no need to ask for an empty page
                break
        if (
            foreign
        ):  # the store ignored type or parent: stop asking in bulk; the sizes left are asked for one by one
            break
    for vid in sorted(wanted - prices.keys(), key=lambda x: (len(x), x)):
        try:
            prices[vid] = _price(client.fetch_json(f"{base}/{vid}").get("prices"))
        except DeadlineReached:
            raise
        except (FetchError, AttributeError) as e:
            if challenged(e) or block_status(e):  # a refusal or a pause, not an unknown price (ingest.run)
                raise
            prices[vid] = None  # unknown beats wrong: no per-gram price for this size


def fetch(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    base = f"https://{roaster.host}/wp-json/wc/store/v1/products"
    raw: list[dict] = []
    prices: dict[str, float | None] = {}
    try:
        for data in _pages(client, base, roaster.host):
            for p in (p for p in data if isinstance(p, dict)):
                raw.append(p)
                if not _needs_variation_prices(p):  # one price for every size: the listing's
                    prices |= dict.fromkeys(_variation_ids(p), _price(p.get("prices")))
        variation_prices(client, base, roaster.host, [p for p in raw if _needs_variation_prices(p)], prices)
    except DeadlineReached as e:
        # Only products read whole: listed, with every size's price. The rest stay as they were (partial).
        done = [parse_product(p, roaster, prices) for p in raw if prices.keys() >= set(_variation_ids(p))]
        raise PartialCatalog(f"{roaster.host}: {e} ({len(done)} of {len(raw)}+ products read)", done) from e
    return [parse_product(p, roaster, prices) for p in raw]
