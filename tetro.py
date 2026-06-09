"""
Tetro scraper (tetro.in) — Shopify-based refurbished iPhone store.

Requests-only (Shopify products.json, no Playwright):
  /collections/all/products.json?limit=250&page=N

Tetro sells only "Pre-loved" (like-new) iPhones — there is NO condition/grade
option. Variants vary by Storage × Battery Health × Warranty Info, and each
COLOR is a separate product ("iPhone 16 (Pink, Pre-loved)"). Battery health and
warranty change the price but aren't a grade.

So we save ONE row per (model, storage) at the LOWEST available price across all
battery-health/warranty variants and across the per-color products (they share a
variant_key). Condition is fixed to "Like New" — the store's own label (all
stock is "Pre-loved" / like-new; there is no grade option).

Options' slot order/spelling vary, so Storage is resolved by NAME via
shopify_option_index (Battery Health / Warranty Info match no role keyword and
are ignored). Products with no Storage option (a few "Title"/"Variant" ones) are
skipped — they can't be keyed reliably.

Price: Shopify products.json price is rupees ("41490.00").
Availability: per-variant `available` flag. Deep-link: /products/<handle>?variant=<id>.

Run with: python3 tetro.py
"""
import re
import time
import requests
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone, shopify_option_index
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer, months_to_days
from obs import init_sentry, log_error

SITE = "tetro"
BASE_URL = "https://tetro.in"
API_URL = f"{BASE_URL}/collections/all/products.json"
DELAY = 0.4
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}
# Tetro lists only like-new ("Pre-loved") stock and exposes no grade option, so
# we record the store's own condition: "Like New".
CONDITION = normalize_condition("Like New")


def get_image(product):
    images = product.get("images", [])
    if images:
        src = images[0].get("src", "")
        if src.startswith("//"):
            src = "https:" + src
        return src or None
    return None


def get_warranty(product):
    for tag in product.get("tags", []):
        m = re.search(r"(\d+)\s*month", str(tag), re.I)
        if m:
            return int(m.group(1))
    return None


def fetch_all_products():
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
        if len(batch) < 250:
            break
        page += 1
        time.sleep(DELAY)
    return products


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)
    print("Fetching all products from Tetro API...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")

    best = {}  # (variant_key) -> lowest-price offer (condition is always Superb)

    for prod in products:
        title = prod.get("title", "")
        model = clean_model(title)
        if not model or not is_phone(model):
            continue

        handle = prod.get("handle", "")
        url = f"{BASE_URL}/products/{handle}"
        img_url = get_image(prod)
        warranty_months = get_warranty(prod)

        variants = prod.get("variants", [])
        if not variants:
            continue
        if not INCLUDE_OOS and not any(v.get("available", False) for v in variants):
            continue

        # Storage slot resolved by name (order/spelling varies).
        size_pos = shopify_option_index(prod).get("size")
        if not size_pos:
            continue  # no Storage option — can't key this product reliably

        for v in variants:
            avail = bool(v.get("available", False))
            if not avail and not INCLUDE_OOS:
                continue
            storage = normalize_storage((v.get(f"option{size_pos}") or "").strip())
            price = float(v.get("price")) if v.get("price") else None
            if not storage or not price:
                continue

            variant_key = make_variant_key(model, storage, None)
            availability = "in_stock" if avail else "out_of_stock"
            variant_id = v.get("id")
            variant_url = f"{url}?variant={variant_id}" if variant_id else url

            if better_offer(availability, price, best.get(variant_key)):
                best[variant_key] = {
                    "model": model, "storage": storage, "ram": None,
                    "variant_key": variant_key, "price": price,
                    "availability": availability, "url": variant_url,
                    "image_url": img_url, "warranty_days": months_to_days(warranty_months),
                    "name": f"{model} {storage}".strip(),
                }

        time.sleep(DELAY)

    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    print(f"\nSaving {len(best)} (variant) offers...")
    saved = 0
    for vkey, o in best.items():
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
            condition=CONDITION, warranty_days=o.get("warranty_days"), url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:32} [{CONDITION}] {o['availability']:12} ₹{o['price']:.0f}")

    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=bool(best))
    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
