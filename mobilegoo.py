"""
MobileGoo scraper (mobilegoo.shop) — refurbished & unboxed phones.

Shopify store — uses products.json API (no Playwright needed).
Two collections:
  /collections/mobiles       — refurbished phones
  /collections/unbox-mobiles — unboxed/pre-owned phones

Variant structure: option1=Color, option2=Storage, option3=Grade+Warranty
  e.g. "Good (3 Months Seller Warranty)" → condition="Good"
Availability: variant.available = True/False (reliable)
Price: in rupees directly (not paise)
Variant URL: /products/{handle}?variant={id}

Groups by (model, storage, condition), keeps lowest price across colors.

Run with: python3 mobilegoo.py
"""
import re
import time
import requests
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone, parse_size_string, shopify_option_index
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer
from obs import init_sentry, log_error

SITE = "mobilegoo"
BASE_URL = "https://mobilegoo.shop"
COLLECTIONS = [
    "mobiles",
    "unbox-mobiles",
]
DELAY = 0.3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def parse_condition(raw):
    """Strip warranty info from condition string.
    e.g. "Good (3 Months Seller Warranty)" → "Good"
         "Superb (6 Months Seller Warra"   → "Superb"
    """
    if not raw:
        return normalize_condition("Refurbished")
    # Take everything before the first parenthesis
    clean = re.sub(r"\s*\(.*$", "", raw).strip()
    return normalize_condition(clean or raw)


def parse_warranty(raw):
    """Return (warranty_months, warranty_label) from the grade label's
    parenthetical:
      "Good (3 Months Seller Warranty)"          → (3, None)
      "Good (7 Day Checking Warranty)"            → (None, "7-day warranty")
      "Superb (9 to 12 Months Brand Warranty)"   → (None, "Brand Warranty")
      "Superb (3 to 6 Months Apple Warranty)"    → (None, "Brand Warranty")
    A manufacturer/Apple/Samsung warranty is shown as "Brand Warranty" (per
    product owner, regardless of stated months). A seller/service warranty with
    a month duration gives months (ranges take the lower bound). A days-only
    checking warranty gives an "N-day warranty" label. (None, None) if absent."""
    if not raw:
        return None, None
    s = str(raw).lower()
    if "warrant" not in s:
        return None, None
    seller = re.search(r"seller|service|store", s)
    if not seller and re.search(r"brand|apple|samsung|manufacturer", s):
        return None, "Brand Warranty"
    rng = re.search(r"(\d+)\s*to\s*(\d+)\s*month", s)
    if rng:
        return int(rng.group(1)), None
    m = re.search(r"(\d+)\s*month", s)
    if m:
        return int(m.group(1)), None
    d = re.search(r"(\d+)\s*day", s)
    if d:
        return None, f"{int(d.group(1))}-day warranty"
    return None, None


def fetch_collection(collection):
    """Fetch all products from a Shopify collection via products.json API."""
    products = []
    page = 1
    while True:
        url = f"{BASE_URL}/collections/{collection}/products.json?limit=250&page={page}"
        r = requests.get(url, headers=HEADERS, timeout=30)
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

    best = {}  # (variant_key, condition) -> lowest price offer

    for collection in COLLECTIONS:
        products = fetch_collection(collection)
        print(f"Collection '{collection}': {len(products)} products")

        for prod in products:
            raw_title = prod.get("title", "")
            model = clean_model(raw_title)

            if not model or not is_phone(model):
                continue

            handle = prod.get("handle", "")
            prod_url = f"{BASE_URL}/products/{handle}"

            # Get first available image
            images = prod.get("images", [])
            prod_img = None
            if images:
                src = images[0].get("src", "")
                if src.startswith("//"):
                    src = "https:" + src
                prod_img = src or None

            # Resolve which option slot holds storage vs grade by NAME (positions
            # vary per store/product). Fall back to MobileGoo's usual layout
            # (storage=option2, grade=option3) when names don't identify a role.
            opt_idx = shopify_option_index(prod)
            storage_pos = opt_idx.get("size", 2)
            cond_pos = opt_idx.get("grade", 3)

            for v in prod.get("variants", []):
                avail = bool(v.get("available", False))
                if not avail and not INCLUDE_OOS:
                    continue

                price = float(v.get("price", 0) or 0)
                if not price:
                    continue

                storage_raw = v.get(f"option{storage_pos}", "") or ""
                condition_raw = v.get(f"option{cond_pos}", "") or ""

                # Parse storage — option2 can be "128GB" or "4GB-64GB" (RAM-Storage)
                ram, storage = parse_size_string(storage_raw.replace("-", "|"))
                if not storage:
                    storage = normalize_storage(storage_raw)

                condition = parse_condition(condition_raw)
                warranty_months, warranty_label = parse_warranty(condition_raw)
                variant_id = v.get("id", "")
                variant_url = f"{prod_url}?variant={variant_id}" if variant_id else prod_url

                # Variant image if available
                img_url = prod_img
                featured = v.get("featured_image")
                if featured and featured.get("src"):
                    src = featured["src"]
                    if src.startswith("//"):
                        src = "https:" + src
                    img_url = src

                vkey = make_variant_key(model, storage, ram)
                bkey = (vkey, condition)
                availability = "in_stock" if avail else "out_of_stock"

                if better_offer(availability, price, best.get(bkey)):
                    best[bkey] = {
                        "model": model, "storage": storage, "ram": ram,
                        "variant_key": vkey, "condition": condition,
                        "price": price, "availability": availability,
                        "url": variant_url, "image_url": img_url,
                        "warranty_months": warranty_months,
                        "warranty_label": warranty_label,
                        "name": f"{model} {storage or ''}".strip(),
                    }

        time.sleep(DELAY)

    print(f"\nUnique (variant, condition) offers: {len(best)}")

    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    saved = 0
    for (vkey, condition), o in best.items():
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
            condition=condition, rating=None, review_count=None,
            warranty_months=o.get("warranty_months"),
            warranty_label=o.get("warranty_label"), url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:40} [{condition:15}] {o['availability']:12} ₹{o['price']:.0f}")

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
