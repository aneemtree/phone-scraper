"""
Gadget Rebirth scraper (gadgetrebirth.com) — custom React storefront, all-brands
refurbished phones.

Requests-only. The React SPA is backed by its own JSON API at
api.gadgetrebirth.com; the catalog endpoint returns the FULL per-product variant
matrix inline, so no per-product fetch and no browser are needed:

  GET https://api.gadgetrebirth.com/api/products?limit=100&skip=<n>

Pagination is by `skip` (the `page`/`limit>200` params are ignored by the API),
so we walk skip in steps of 100 until a short/empty page. The endpoint returns
ALL products (live + sold-out historical, ~1100), across every category — we keep
category == "phones" only.

Variant matrix (standard approach): each product has variants[] with
options{Condition, Storage, Color}, an integer rupee `price`, `compareAtPrice`
(strike, ignored), `stock`, and `active`. For every (condition, storage) we take
the LOWEST price across colors and save one row per (variant_key, condition).

Availability rule: a variant is buyable iff `active AND stock>0`. Validated
against the rendered site — the payload carries the phantom-inventory cases this
guards against: `active=false, stock>0` (sold-out but stocked) and
`active=true, stock=0` are BOTH excluded, matching the Add-to-Cart UI. The raw
`stock`/top-level `status` are NOT trusted on their own.

Conditions: New / Like New / Excellent / Good / Fair (store grades; Good/Fair
share the Cashify vocab, Like New matches Tetro, Excellent/New are store-specific).
Mapped + typo-fixed via norm_condition().

Price: variant.price is rupees (int). Image: product main image (R2-hosted on
first sight). Deep-link: /product/<sku>/ (the SPA has no per-variant URL param).
OOS variants are saved only in the monthly catalog pass (INCLUDE_OOS).

Run:  python3 gadgetrebirth.py          # scrape + save
      python3 gadgetrebirth.py --dry     # fetch + print offers, NO DB (validation)
"""
import re
import sys
import time
import requests

from normalize import clean_model, make_variant_key, normalize_storage, normalize_condition, is_phone

SITE = "gadgetrebirth"
BASE_URL = "https://www.gadgetrebirth.com"
API_URL = "https://api.gadgetrebirth.com/api/products"
PAGE = 100
DELAY = 0.3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}

# Gadget Rebirth's grade labels. Keys are lowercased/typo-folded; values are the
# canonical condition strings we store. "good"/"fair" align with the Cashify
# vocab, "Like New" with Tetro; "Excellent"/"New" are store-specific.
CONDITION_MAP = {
    "new": "New",
    "like-new": "Like New",
    "ike-new": "Like New",   # observed payload typo (missing leading 'l')
    "excellent": "Excellent",
    "good": "Good",
    "fair": "Fair",
}


def norm_condition(raw):
    """Map a raw variant Condition to a canonical label, tolerating the payload's
    casing noise and typos ('Like-new', 'ike-new', 'Excellent')."""
    c = (raw or "").strip().lower().replace("–", "-")
    return CONDITION_MAP.get(c) or normalize_condition(raw)


def build_model(brand, name):
    """Build a clean model name. The API `name` sometimes carries the brand/
    sub-brand ('iPhone 17', 'OnePlus 12') and sometimes not ('Galaxy S25 Ultra',
    'Xperia 1 V'); prepend the brand slug only when its token is absent so
    clean_model can canonicalize the brand chip without doubling it."""
    nm = name or ""
    # On this API the `name` carries no storage/colour — any parenthesised token is
    # the model identifier ('Phone (2)', '(2a)'), which clean_model would otherwise
    # delete. Unwrap parens first so the identifier survives.
    nm = re.sub(r"\(([^)]*)\)", r" \1 ", nm)
    raw = nm if (brand and brand.lower() in nm.lower()) else f"{brand} {nm}".strip()
    return clean_model(raw)


def get_image(product):
    for img in (product.get("images") or []):
        if img.get("main") and img.get("url"):
            return img["url"]
    return product.get("image") or None


def fetch_all_products():
    """Walk the catalog by `skip` until a short/empty page (the API caps a single
    response at ~200 rows and ignores page/large-limit, so skip is the only
    reliable cursor)."""
    products, skip = [], 0
    while True:
        r = requests.get(API_URL, params={"limit": PAGE, "skip": skip},
                         headers=HEADERS, timeout=40)
        if r.status_code != 200:
            print(f"  API error {r.status_code} at skip={skip}")
            break
        batch = r.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        print(f"  fetched {len(products)} products so far (skip {skip})...")
        if len(batch) < PAGE:
            break
        skip += PAGE
        time.sleep(DELAY)
    return products


def _better_offer(new_availability, new_price, cur):
    """In-stock beats out-of-stock; within the same availability the lower price
    wins. Local copy of db.better_offer so build_offers stays DB-free (the --dry
    validation path must run without Supabase creds)."""
    if cur is None:
        return True
    new_in = new_availability == "in_stock"
    cur_in = cur.get("availability") == "in_stock"
    if new_in != cur_in:
        return new_in
    return new_price < cur["price"]


def build_offers(products, include_oos=False):
    """Pure parse: products -> {(variant_key, condition): offer}. No DB.
    Keeps the LOWEST color price per (condition, storage); prefers in-stock over
    out-of-stock, then lower price (mirrors db.better_offer)."""
    best = {}
    for prod in products:
        if prod.get("category") != "phones":
            continue
        model = build_model(prod.get("brand", ""), prod.get("name", ""))
        if not model or not is_phone(model):
            continue
        url = f"{BASE_URL}/product/{prod.get('sku', '')}/"
        img_url = get_image(prod)

        variants = prod.get("variants") or []
        groups = {}  # (condition, storage) -> {"in": [prices], "oos": [prices]}
        for v in variants:
            opts = v.get("options") or {}
            cond = norm_condition(opts.get("Condition"))
            storage = normalize_storage(opts.get("Storage"))
            price = v.get("price")
            if not cond or not storage or not price:
                continue
            available = bool(v.get("active")) and (v.get("stock") or 0) > 0
            if not available and not include_oos:
                continue
            g = groups.setdefault((cond, storage), {"in": [], "oos": []})
            (g["in"] if available else g["oos"]).append(float(price))

        for (cond, storage), g in groups.items():
            if g["in"]:
                price, availability = min(g["in"]), "in_stock"
            elif g["oos"]:
                price, availability = min(g["oos"]), "out_of_stock"
            else:
                continue
            variant_key = make_variant_key(model, storage)
            bkey = (variant_key, cond)
            if _better_offer(availability, price, best.get(bkey)):
                best[bkey] = {
                    "model": model, "storage": storage, "variant_key": variant_key,
                    "condition": cond, "price": price, "availability": availability,
                    "url": url, "image_url": img_url,
                    "name": f"{model} {storage}".strip(),
                }
    return best


def scrape():
    from datetime import datetime, timezone
    from db import (save_phone, save_price, ensure_image, mark_site_oos,
                    mark_unseen_out_of_stock, INCLUDE_OOS)

    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)
    print("Fetching all products from Gadget Rebirth API...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")

    best = build_offers(products, include_oos=INCLUDE_OOS)
    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}

    print(f"\nSaving {len(best)} (variant, condition) offers...")
    saved = 0
    for (vkey, cond), o in best.items():
        hosted = None
        if o["image_url"]:
            hosted = ensure_image(o["image_url"], f"{SITE}/{o['variant_key']}.jpg")
        final_image = hosted or o["image_url"]

        pid = save_phone(
            SITE, o["name"], o["url"], final_image,
            o["model"], o["storage"], None, o["variant_key"],
            in_stock=(o["name"] in in_stock_names),
        )
        save_price(pid, o["price"], availability=o["availability"],
                   condition=cond, url=o["url"])
        saved += 1
        print(f"  saved: {o['name']:32} [{cond:10}] {o['availability']:12} ₹{o['price']:.0f}")

    mark_unseen_out_of_stock(SITE, run_started_at)
    print(f"\nDone. Saved {saved} (variant, condition) offers from {SITE}.")


def dry_run():
    """Fetch + parse + print, no DB writes / no creds needed."""
    products = fetch_all_products()
    phones = [p for p in products if p.get("category") == "phones"]
    print(f"\nTotal products: {len(products)} | phones: {len(phones)}")
    best = build_offers(products, include_oos=False)
    print(f"Available (variant, condition) offers: {len(best)}\n")
    for (vkey, cond), o in sorted(best.items(), key=lambda kv: kv[0]):
        print(f"  {o['name']:34} [{cond:10}] {o['availability']:12} ₹{o['price']:.0f}  {vkey}")


if __name__ == "__main__":
    if "--dry" in sys.argv:
        dry_run()
    else:
        from obs import init_sentry, log_error
        init_sentry(SITE)
        try:
            scrape()
        except Exception as e:
            log_error(e, site=SITE, phase="scrape")
            raise
