"""
iTradeit scraper (itradeit.in) — WordPress/WooCommerce, requests-only.

Two product categories, each fixing the CONDITION (there is no grade/condition
variant axis — the axes are only pa_color × pa_storage, so condition comes from
category membership like CellBuddy):
  - open-box-phones      (id 438, "Open Box Phones")   -> "Open Box"
  - certified-refurbished(id 60,  "Refurbished Phones") -> "Unknown Condition"

Data sources (no Playwright):
  - Listing + metadata from the public Store API
    (/wp-json/wc/store/v1/products?category=<id>).
  - Per-variant price/stock/image from the product page's embedded
    `data-product_variations` JSON. Matrices are tiny (color×storage, a handful
    each) — well under WooCommerce's ajax threshold — so the attribute is always
    inlined and parseable; no ?wc-ajax fallback is needed. A simple/no-form
    product falls back to the Store API advertised price.

Storage bundles RAM: the pa_storage terms are "12GB/256GB", "8GB/128GB" (slug
"12gb-256gb"). Like oldsold, the same storage can appear at different RAM/price,
so we key the dedup dict by (variant_key, ram, condition) and fold RAM into the
saved `name`; make_variant_key stays storage-only so the phone still groups
cross-store. Cross-store note: itradeit drops "Galaxy" from Samsung titles
("Samsung S25 Ultra") — clean_model re-inserts it so the key matches other stores.

Prices: embedded display_price is rupees (display_regular_price is the strike,
ignored). Store API prices.price is minor units (÷100). Availability: per-variation
is_in_stock / availability_html, gated by the product's is_purchasable + stock
badge. Deep-link: permalink + ?attribute_pa_storage=<slug>&attribute_pa_color=<slug>.

Run with: python3 itradeit.py
"""
import re
import html
import time
import json
import requests
from normalize import (clean_model, normalize_storage, make_variant_key,
                       parse_size_string, is_phone)
from db import (save_phone, save_price, ensure_image, mark_site_oos,
                mark_unseen_out_of_stock, INCLUDE_OOS, better_offer)
from obs import init_sentry, log_error

SITE = "itradeit"
BASE_URL = "https://itradeit.in"
# category id -> condition label (condition is carried by category membership)
CATEGORIES = {
    438: "Open Box",          # open-box-phones
    60:  "Unknown Condition", # certified-refurbished ("Refurbished Phones")
}
DELAY = 0.3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def fetch_listing(category_id):
    products, page = [], 1
    while True:
        url = (f"{BASE_URL}/wp-json/wc/store/v1/products"
               f"?category={category_id}&per_page=100&page={page}")
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(DELAY)
    return products


def storage_map(prod):
    """pa_storage slug -> raw term name ('12gb-256gb' -> '12GB/256GB')."""
    out = {}
    for a in (prod.get("attributes") or []):
        if a.get("taxonomy") == "pa_storage":
            for t in (a.get("terms") or []):
                out[t.get("slug")] = t.get("name")
    return out


def _img(src):
    if not src:
        return None
    if src.startswith("//"):
        src = "https:" + src
    return src


def variation_in_stock(v):
    """Per-variation availability: prefer the boolean flag, else parse the badge."""
    if "is_in_stock" in v:
        return bool(v.get("is_in_stock"))
    ah = v.get("availability_html") or ""
    return "out-of-stock" not in ah


def parse_embedded(page, smap):
    """Embedded data-product_variations -> list of variant dicts, or None."""
    m = re.search(r'data-product_variations="(.*?)"', page, re.S)
    if not m:
        return None
    val = m.group(1)
    if val.strip() in ("false", "False", '"false"'):
        return None  # above the ajax threshold (not expected for this store)
    try:
        raw = json.loads(html.unescape(val))
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, list):
        return None
    out = []
    for v in raw:
        attrs = v.get("attributes") or {}
        sslug = attrs.get("attribute_pa_storage")
        ram, storage = parse_size_string(smap.get(sslug) or sslug or "")
        price = v.get("display_price")
        if not storage or price in (None, ""):
            continue
        img = (v.get("image") or {})
        out.append({
            "ram": ram, "storage": storage, "storage_slug": sslug,
            "color_slug": attrs.get("attribute_pa_color"),
            "price": float(price),
            "in_stock": variation_in_stock(v),
            "image_url": _img(img.get("full_src") or img.get("src")),
        })
    return out


def store_api_single(prod, smap):
    """Fallback: advertised Store API min price at the cheapest storage term."""
    try:
        price = float((prod.get("prices") or {}).get("price")) / 100.0
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return []
    terms = [t for a in (prod.get("attributes") or [])
             if a.get("taxonomy") == "pa_storage" for t in (a.get("terms") or [])]

    def gb(t):
        _, s = parse_size_string(smap.get(t.get("slug")) or t.get("name") or "")
        m = re.search(r"(\d+)(TB|GB)", s or "")
        return (int(m.group(1)) * (1024 if m.group(2) == "TB" else 1)) if m else 10**9

    term = min(terms, key=gb) if terms else None
    if term:
        sslug = term.get("slug")
        ram, storage = parse_size_string(smap.get(sslug) or term.get("name") or "")
    else:
        sslug, ram, storage = None, None, None
    if not storage:
        return []
    badge = (prod.get("stock_availability") or {}).get("class", "")
    in_stock = bool(prod.get("is_purchasable")) and badge == "in-stock"
    images = prod.get("images") or []
    return [{"ram": ram, "storage": storage, "storage_slug": sslug,
             "color_slug": None, "price": price, "in_stock": in_stock,
             "image_url": _img(images[0].get("src")) if images else None}]


def variations_for(prod, smap):
    """Return the per-variant offers for a product (embedded, else fallback)."""
    if prod.get("type") == "variable":
        try:
            page = requests.get(f"{BASE_URL}/product/{prod.get('slug','')}/",
                                headers=HEADERS, timeout=30).text
        except Exception:
            page = ""
        embedded = parse_embedded(page, smap)
        if embedded:
            return embedded
    return store_api_single(prod, smap)


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)

    best = {}  # (variant_key, ram, condition) -> lowest-price offer

    for category_id, condition in CATEGORIES.items():
        products = fetch_listing(category_id)
        print(f"Fetched {len(products)} products in category {category_id} "
              f"-> condition {condition!r}")

        for prod in products:
            model = clean_model(prod.get("name", ""))
            if not model or not is_phone(model):
                continue

            badge = (prod.get("stock_availability") or {}).get("class", "")
            product_in_stock = bool(prod.get("is_purchasable")) and badge == "in-stock"
            if not product_in_stock and not INCLUDE_OOS:
                continue

            permalink = prod.get("permalink") or f"{BASE_URL}/product/{prod.get('slug','')}/"
            images = prod.get("images") or []
            prod_img = _img(images[0].get("src")) if images else None
            smap = storage_map(prod)

            # WooCommerce Store API carries native, per-product review data.
            # Only keep it when there are real reviews (count > 0).
            rating = float(prod.get("average_rating") or 0) or None
            review_count = int(prod.get("review_count") or 0) or None
            if not review_count:
                rating = None

            for v in variations_for(prod, smap):
                if not v["in_stock"] and not INCLUDE_OOS:
                    continue
                ram, storage, price = v["ram"], v["storage"], v["price"]
                vkey = make_variant_key(model, storage, ram)
                availability = "in_stock" if v["in_stock"] else "out_of_stock"
                bkey = (vkey, ram, condition)
                if not better_offer(availability, price, best.get(bkey)):
                    continue

                params = []
                if v.get("storage_slug"):
                    params.append(f"attribute_pa_storage={v['storage_slug']}")
                if v.get("color_slug"):
                    params.append(f"attribute_pa_color={v['color_slug']}")
                url = f"{permalink}?{'&'.join(params)}" if params else permalink

                best[bkey] = {
                    "model": model, "storage": storage, "ram": ram,
                    "variant_key": vkey, "condition": condition,
                    "price": price, "availability": availability,
                    "url": url, "image_url": v.get("image_url") or prod_img,
                    "rating": rating, "review_count": review_count,
                    "name": (f"{model} {ram}/{storage}" if ram and storage
                             else f"{model} {storage}").strip(),
                }

    print(f"\nUnique (variant, condition) offers: {len(best)}")

    # A phone (site+name, which folds RAM) is in stock if any of its offers is.
    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    saved = 0
    for o in best.values():
        hosted = None
        if o["image_url"]:
            dest = f"{SITE}/{o['variant_key']}.jpg".replace("|", "_")
            hosted = ensure_image(o["image_url"], dest)
        final_image = hosted or o["image_url"]

        pid = save_phone(
            SITE, o["name"], o["url"], final_image,
            o["model"], o["storage"], o["ram"], o["variant_key"],
            in_stock=(o["name"] in in_stock_names),
        )
        save_price(
            pid, o["price"], availability=o["availability"],
            condition=o["condition"], url=o["url"],
            rating=o.get("rating"), review_count=o.get("review_count"),
        )
        saved += 1
        print(f"  saved: {o['name']:42} [{o['condition']:18}] ₹{o['price']:.0f}  [{o['availability']}]")

    mark_unseen_out_of_stock(SITE, run_started_at)
    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
