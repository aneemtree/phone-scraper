"""
OldSold scraper (oldsold.in) — refurbished phones.

Shopify store — uses products.json API (no Playwright needed).
Single collection (/products.json) holds ALL products; non-phones filtered via is_phone().

Variant options are detected BY NAME (order varies between products):
  "RAM/Storage"        → e.g. "4GB/128GB"  → ram=4GB, storage=128GB
  "Physical Condition" → e.g. "Excellent"  → condition
  "Warranty"           → e.g. "7 Days"     → ignored (appended info only)

Availability: variant.available = True/False
Price: rupees directly (Shopify products.json), not paise
Variant URL: /products/{handle}?variant={id}

Groups by (model, storage, condition), keeps lowest price across colors/warranties.

Run with: python3 oldsold.py
"""
import re
import time
import requests
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone, parse_size_string
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer
from obs import init_sentry, log_error

SITE = "oldsold"
BASE_URL = "https://oldsold.in"
DELAY = 0.3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def parse_ram_storage(value):
    """Parse 'RAM/Storage' option like '4GB/128GB', '8GB/256GB', or just '128GB'.
    Returns (ram, storage).
    """
    if not value:
        return None, None
    value = value.strip()
    # Slash format: "4GB/128GB"
    slash = re.search(r"^(\d+)\s*GB\s*/\s*(\d+)\s*(GB|TB)?$", value, re.I)
    if slash:
        ram = f"{slash.group(1)}GB"
        storage = normalize_storage(f"{slash.group(2)}{(slash.group(3) or 'GB').upper()}")
        return ram, storage
    # Just storage with GB/TB
    if re.search(r"\d+\s*(GB|TB)", value, re.I):
        return parse_size_string(value)
    # Bare number → storage GB
    if re.match(r"^\d+$", value):
        return None, normalize_storage(f"{value}GB")
    return None, None


def get_option_value(product, variant, target_names):
    """Find a variant's option value by matching the product's option NAMES.
    target_names: list of acceptable names (lowercased substrings).
    Returns the matched option value string, or None.
    """
    options = product.get("options", [])  # [{name, position, values}]
    for opt in options:
        name = (opt.get("name") or "").strip().lower()
        if any(t in name for t in target_names):
            pos = opt.get("position", 1)  # 1-indexed
            return variant.get(f"option{pos}")
    return None


def fetch_all_products():
    """Fetch all products from Shopify products.json API, paginated."""
    products = []
    page = 1
    while True:
        url = f"{BASE_URL}/products.json?limit=250&page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except Exception:
            break
        if r.status_code != 200:
            break
        batch = r.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        page += 1
        time.sleep(DELAY)
    return products


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)

    products = fetch_all_products()
    print(f"Fetched {len(products)} products total")

    best = {}  # (variant_key, condition) -> lowest price offer

    for prod in products:
        raw_title = prod.get("title", "")
        model = clean_model(raw_title)

        if not model or not is_phone(model, raw_title):
            continue

        handle = prod.get("handle", "")
        prod_url = f"{BASE_URL}/products/{handle}"

        # Product-level image fallback
        images = prod.get("images", [])
        prod_img = None
        if images:
            src = images[0].get("src", "")
            if src.startswith("//"):
                src = "https:" + src
            prod_img = src or None

        for v in prod.get("variants", []):
            avail = bool(v.get("available", False))
            if not avail and not INCLUDE_OOS:
                continue

            price = float(v.get("price", 0) or 0)
            if not price:
                continue

            # Detect option values by NAME (order varies)
            rs_value = get_option_value(prod, v, ["ram", "storage", "memory"])
            cond_value = get_option_value(prod, v, ["condition", "grade"])

            ram, storage = parse_ram_storage(rs_value)
            condition = normalize_condition(cond_value) if cond_value else normalize_condition("Refurbished")

            if not storage:
                continue

            variant_id = v.get("id", "")
            variant_url = f"{prod_url}?variant={variant_id}" if variant_id else prod_url

            # Variant image if present
            img_url = prod_img
            featured = v.get("featured_image")
            if featured and featured.get("src"):
                src = featured["src"]
                if src.startswith("//"):
                    src = "https:" + src
                img_url = src

            vkey = make_variant_key(model, storage, ram)
            # Key by RAM as well: oldsold sells distinct RAM variants at the same
            # storage (e.g. 8GB/256GB vs 12GB/256GB at different prices).
            # make_variant_key excludes RAM (kept storage-only for cross-store
            # grouping), so without RAM in the key the two variants would collapse
            # into one offer. RAM is also folded into the name so save_phone (keyed
            # on site+name) stores them as separate rows.
            bkey = (vkey, ram, condition)
            availability = "in_stock" if avail else "out_of_stock"

            if better_offer(availability, price, best.get(bkey)):
                best[bkey] = {
                    "model": model, "storage": storage, "ram": ram,
                    "variant_key": vkey, "condition": condition,
                    "price": price, "availability": availability,
                    "url": variant_url, "image_url": img_url,
                    "name": (f"{model} {ram}/{storage}" if ram and storage
                             else f"{model} {storage or ''}").strip(),
                }

    print(f"\nUnique (variant, condition) offers: {len(best)}")

    # A phone (site+name, which includes RAM) is in stock if any of its offers is.
    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    saved = 0
    for o in best.values():
        condition = o["condition"]
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
            condition=condition, rating=None, review_count=None, url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:40} [{condition:15}] ₹{o['price']:.0f}")

    # Phones not seen in this run -> out of stock (guarded against partial runs).
    mark_unseen_out_of_stock(SITE, run_started_at)

    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise