"""
Maple Store scraper (maplestore.in) — Shopify, iPhone-only, pre-owned.

Maple Store uses a CUSTOM variant-group app, NOT Shopify variants: every
products.json entry is ONE physical unit (variant.sku = the device serial), all
"Default Title". Model/storage/colour are baked into the product TITLE
("iPhone 16 Pro Max - 256GB - Desert Titanium - IW (28-Jun-26) - Pre-owned"), but
the per-unit GRADE is NOT in the title/tags — it's rendered into the product page
HTML as the active condition swatch:

  <div class="option_main_container_condition"> ...
    <div class="cstm_condition_value ... active_value" data_val="superb"> Superb </div>

So: list every unit from products.json, then fetch each product page (threaded,
polite — the site 429s under load) to read the active grade. The page grades are
Almost New / Superb / Good / Fair (data_val "fiar" is the store's typo for Fair);
"Almost New" is mapped to "Like New" (shared cross-store label), and Superb/Good/
Fair share the Cashify vocab.

Title separators are inconsistent (' - ' vs tight 'Pro Max-256GB-...'), so model
and storage are anchored on the storage token (\d+GB/TB, present in every title):
storage = that token, model = everything before it (clean_model strips the trailing
dash, plus 'Dual'/'E-Sim'/colour/IW noise). condition comes from the page only.

Standard approach: group by (variant_key, grade), keep the LOWEST price across the
colour/warranty units, one row per (variant_key, grade). Price is rupees
("84999.00"); availability = variant.available; deep-link /products/<handle>?variant=<id>.
OOS units saved only in the monthly catalog pass (INCLUDE_OOS).

Run:  python3 maplestore.py          # scrape + save
      python3 maplestore.py --dry     # fetch + print offers, NO DB (validation)
"""
import re
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor

from normalize import clean_model, make_variant_key, normalize_storage, is_phone

SITE = "maplestore"
BASE_URL = "https://maplestore.in"
COLLECTION = "/collections/all-iphones/products.json"
WORKERS = 4          # polite — the site 429s above this
DELAY = 0.3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# data_val (lowercased) -> canonical grade. "fiar" is the store's own typo.
GRADE_MAP = {
    "almostnew": "Like New",
    "superb": "Superb",
    "good": "Good",
    "fair": "Fair",
    "fiar": "Fair",
}

_session = requests.Session()
_session.headers.update({"User-Agent": UA})


def _fetch(url, params=None, tries=5):
    """GET with backoff on 429 / 5xx (the store rate-limits page fetches)."""
    delay = 1.0
    r = None
    for _ in range(tries):
        r = _session.get(url, params=params, timeout=40)
        if r.status_code == 200:
            return r
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(delay)
            delay = min(delay * 2, 16)
            continue
        return r
    return r


def fetch_all_products():
    products, page = [], 1
    while True:
        r = _fetch(BASE_URL + COLLECTION, params={"limit": 250, "page": page})
        if not r or r.status_code != 200:
            print(f"  API error at page {page}")
            break
        batch = r.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        print(f"  fetched {len(products)} units so far (page {page})...")
        if len(batch) < 250:
            break
        page += 1
        time.sleep(DELAY)
    return products


def parse_model_storage(title):
    """Anchor on the storage token (present in every title); model is whatever
    precedes it. Returns (model, storage) or (None, None)."""
    m = re.search(r"(\d+)\s*(GB|TB)", title, re.I)
    if not m:
        return None, None
    storage = normalize_storage(m.group(0))
    model = clean_model(title[:m.start()])
    return model, storage


def grade_for(handle):
    """Read the active condition swatch from the product page. None if not found."""
    r = _fetch(f"{BASE_URL}/products/{handle}")
    if not r or r.status_code != 200:
        return None
    html = r.text
    i = html.find("option_main_container_condition")
    if i < 0:
        return None
    # Bound the search to the condition container only — the next option container
    # (storage/color) starts at the following "option_main_container", so without
    # this bound a unit whose condition has no active swatch would bleed into the
    # storage block and mis-read e.g. "256gb" as the grade.
    j = html.find("option_main_container", i + len("option_main_container_condition"))
    blk = html[i:j] if j > i else html[i:i + 1400]
    # Only accept a recognized grade data_val; anything else → None (→ "Pre-owned").
    for m in re.finditer(r'active_value"\s+data_val="([^"]+)"', blk):
        g = GRADE_MAP.get(m.group(1).strip().lower())
        if g:
            return g
    return None


def fetch_grades(products):
    """Threaded, polite per-unit grade lookup. handle -> grade."""
    handles = [p["handle"] for p in products]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(grade_for, handles))
    return dict(zip(handles, results))


def _better_offer(new_availability, new_price, cur):
    """In-stock beats out-of-stock; within the same availability the lower price
    wins. Local copy of db.better_offer so build_offers stays DB-free for --dry."""
    if cur is None:
        return True
    new_in = new_availability == "in_stock"
    cur_in = cur.get("availability") == "in_stock"
    if new_in != cur_in:
        return new_in
    return new_price < cur["price"]


def build_offers(products, grades, include_oos=False):
    """products + {handle: grade} -> {(variant_key, grade): offer}. Lowest price
    per (model, storage, grade) across colour/warranty units; in-stock preferred."""
    best = {}
    for p in products:
        model, storage = parse_model_storage(p.get("title", ""))
        if not model or not storage or not is_phone(model):
            continue
        grade = grades.get(p["handle"]) or "Pre-owned"
        v = (p.get("variants") or [{}])[0]
        price = float(v["price"]) if v.get("price") else None
        if not price:
            continue
        available = bool(v.get("available"))
        if not available and not include_oos:
            continue
        availability = "in_stock" if available else "out_of_stock"
        vid = v.get("id")
        url = f"{BASE_URL}/products/{p['handle']}" + (f"?variant={vid}" if vid else "")
        img = (p.get("images") or [{}])[0].get("src")
        variant_key = make_variant_key(model, storage)
        bkey = (variant_key, grade)
        if _better_offer(availability, price, best.get(bkey)):
            best[bkey] = {
                "model": model, "storage": storage, "variant_key": variant_key,
                "grade": grade, "price": price, "availability": availability,
                "url": url, "image_url": img,
                "name": f"{model} {storage}".strip(),
            }
    return best


def scrape():
    from datetime import datetime, timezone
    from db import (save_phone, save_price, ensure_image, mark_site_oos,
                    mark_unseen_out_of_stock, INCLUDE_OOS)

    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)
    print("Fetching all units from Maple Store...")
    products = fetch_all_products()
    print(f"\nTotal units: {len(products)}. Fetching per-unit grades...")
    grades = fetch_grades(products)

    best = build_offers(products, grades, include_oos=INCLUDE_OOS)
    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}

    print(f"\nSaving {len(best)} (variant, grade) offers...")
    saved = 0
    for (vkey, grade), o in best.items():
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
                   condition=grade, url=o["url"])
        saved += 1
        print(f"  saved: {o['name']:32} [{grade:11}] {o['availability']:12} ₹{o['price']:.0f}")

    mark_unseen_out_of_stock(SITE, run_started_at)
    print(f"\nDone. Saved {saved} (variant, grade) offers from {SITE}.")


def dry_run():
    """Fetch + parse + print, no DB writes / no creds needed."""
    products = fetch_all_products()
    print(f"\nTotal units: {len(products)}. Fetching grades...")
    grades = fetch_grades(products)
    miss = sum(1 for p in products if not grades.get(p["handle"]))
    best = build_offers(products, grades, include_oos=False)
    print(f"Units with no grade found (→ 'Pre-owned'): {miss}")
    print(f"Available (variant, grade) offers: {len(best)}\n")
    import collections
    print("grade breakdown:", dict(collections.Counter(g for (_, g) in best)))
    print()
    for (vkey, grade), o in sorted(best.items()):
        print(f"  {o['name']:34} [{grade:11}] {o['availability']:12} ₹{o['price']:.0f}  {vkey}")


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
