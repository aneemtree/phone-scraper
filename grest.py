"""
Grest scraper (grest.in) — Shopify-based refurbished iPhone store.

Requests-only (Shopify products.json, no Playwright):
  /collections/iphones/products.json?limit=250&page=N

Options are Storage / Condition / Color (one product has a stray leading space
" Condition"); resolved by NAME via shopify_option_index, so slot order/spelling
don't matter. Conditions are Fair/Good/Superb — the same vocabulary as Cashify,
so offers group cleanly cross-store. Titles omit the "Apple" brand ("iPhone 17"),
which clean_model auto-prefixes to "Apple iPhone 17".

Strategy (standard): group variants by (condition, storage), keep the LOWEST
price across colors, one row per (variant_key, condition). OOS variants are saved
only in the monthly catalog pass (INCLUDE_OOS).

Price: Shopify products.json price is rupees ("67399.00").
Availability: per-variant `available` flag. Deep-link: /products/<handle>?variant=<id>.

Note: this store's collection products.json caps the page size (~30) regardless of
the limit param, so we paginate until an empty page instead of stopping at <250.

Run with: python3 grest.py
"""
import time
import requests
from normalize import clean_model, make_variant_key, parse_size_string, normalize_condition, is_phone, shopify_option_index
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer
from obs import init_sentry, log_error

SITE = "grest"
BASE_URL = "https://grest.in"
API_URL = f"{BASE_URL}/collections/iphones/products.json"
DELAY = 0.4
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def get_image(product):
    images = product.get("images", [])
    if images:
        src = images[0].get("src", "")
        if src.startswith("//"):
            src = "https:" + src
        return src or None
    return None


def fetch_all_products():
    """Paginate until an empty page — the collection endpoint caps page size
    below the requested limit, so a `< limit` check would stop early."""
    products, page = [], 1
    while True:
        r = requests.get(API_URL, params={"limit": 250, "page": page},
                         headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"  API error {r.status_code} at page {page}")
            break
        batch = r.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        print(f"  fetched {len(products)} products so far (page {page})...")
        page += 1
        time.sleep(DELAY)
    return products


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)
    print("Fetching all products from Grest API...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")

    best = {}  # (variant_key, condition) -> lowest-price offer

    for prod in products:
        title = prod.get("title", "")
        model = clean_model(title)
        if not model or not is_phone(model):
            continue

        handle = prod.get("handle", "")
        url = f"{BASE_URL}/products/{handle}"
        img_url = get_image(prod)

        variants = prod.get("variants", [])
        if not variants:
            continue
        if not INCLUDE_OOS and not any(v.get("available", False) for v in variants):
            continue

        opt_idx = shopify_option_index(prod)
        grade_pos = opt_idx.get("grade")
        size_pos = opt_idx.get("size")

        groups = {}  # (grade, size) -> {"in": [...], "oos": [...]}
        for v in variants:
            avail = bool(v.get("available", False))
            if not avail and not INCLUDE_OOS:
                continue
            grade_raw = (v.get(f"option{grade_pos}") if grade_pos else "") or ""
            grade = normalize_condition(grade_raw.strip()) or normalize_condition("Refurbished")
            size = ((v.get(f"option{size_pos}") if size_pos else "")
                    or v.get("option1") or "").strip()
            price = float(v.get("price")) if v.get("price") else None
            if not price or not size:
                continue
            g = groups.setdefault((grade, size), {"in": [], "oos": []})
            (g["in"] if avail else g["oos"]).append((price, v.get("id")))

        if not groups:
            continue

        for (grade, size), g in groups.items():
            ram, storage = parse_size_string(size)
            if not storage:
                continue
            variant_key = make_variant_key(model, storage, ram)
            if g["in"]:
                lowest_price, variant_id = min(g["in"], key=lambda pv: pv[0])
                availability = "in_stock"
            elif g["oos"]:
                lowest_price, variant_id = min(g["oos"], key=lambda pv: pv[0])
                availability = "out_of_stock"
            else:
                continue
            variant_url = f"{url}?variant={variant_id}" if variant_id else url

            bkey = (variant_key, grade)
            if better_offer(availability, lowest_price, best.get(bkey)):
                best[bkey] = {
                    "model": model, "storage": storage, "ram": ram,
                    "variant_key": variant_key, "grade": grade,
                    "price": lowest_price, "availability": availability,
                    "url": variant_url, "image_url": img_url,
                    "name": f"{model} {storage or ''}".strip(),
                }

        time.sleep(DELAY)

    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    print(f"\nSaving {len(best)} (variant, grade) offers...")
    saved = 0
    for (vkey, grade), o in best.items():
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
            condition=grade, url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:32} [{grade:12}] {o['availability']:12} ₹{o['price']:.0f}")

    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=bool(best))
    print(f"\nDone. Saved {saved} (variant, grade) offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
