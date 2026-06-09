"""
EasyPhones scraper (easyphones.co.in) — Shopify-based refurbished phone store.

Requests-only (Shopify products.json, no Playwright):
  /collections/all-collection/products.json?limit=250&page=N

Each product has a variants array. Option NAMES vary per product
(Color/Colour, Storage, Condition/Grade) and their slot order varies too, so we
resolve roles by name via shopify_option_index() rather than a fixed position:
  grade  <- "Condition" / "Grade"   (values like "Superb (Like-New)")
  size   <- "Storage"               ("64 GB", "256 GB", "1 TB")
  color  <- "Color" / "Colour"      (doesn't affect identity)

Condition: the parenthetical qualifier is stripped ("Superb (Like-New)" ->
"Superb") so grades match the Fair/Good/Superb vocabulary used by other stores.

Price: Shopify products.json price is in rupees already (e.g. "21299.00").
Availability: per-variant `available` flag (Shopify's rendered buy state).
Deep-link: /products/<handle>?variant=<id>.

Strategy (standard): group variants by (grade, size), keep the LOWEST price
across available colors, one row per (variant_key, grade). OOS variants are saved
only in the monthly catalog pass (INCLUDE_OOS).

Run with: python3 easyphones.py
"""
import re
import time
import requests
from normalize import clean_model, make_variant_key, parse_size_string, normalize_condition, is_phone, shopify_option_index
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer, months_to_days
from obs import init_sentry, log_error

SITE = "easyphones"
BASE_URL = "https://easyphones.co.in"
API_URL = f"{BASE_URL}/collections/all-collection/products.json"
DELAY = 0.4
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def clean_grade(raw):
    """'Superb (Like-New)' -> 'Superb'; normalize to title case so grades match
    the shared Fair/Good/Superb vocabulary across stores. EasyPhones equates its
    top grade with "Like-New" (it writes "Superb (Like-New)"), so a bare
    "Like-New" is folded into "Superb" too."""
    g = re.sub(r"\(.*?\)", " ", raw or "")
    cond = normalize_condition(g.strip())
    if cond and cond.replace("-", " ").lower() == "like new":
        return normalize_condition("Superb")
    return cond


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
    m = re.search(r"(\d+)\s*month[s]?\s*warranty", product.get("body_html", ""), re.I)
    return int(m.group(1)) if m else 6  # EasyPhones advertises a 6-month warranty


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
    print("Fetching all products from EasyPhones API...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")

    best = {}  # (variant_key, grade) -> lowest-price offer

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

        # Resolve option slots by NAME (order/spelling varies per product).
        opt_idx = shopify_option_index(prod)
        grade_pos = opt_idx.get("grade")
        size_pos = opt_idx.get("size")

        groups = {}  # (grade, size) -> {"in": [...], "oos": [...]}
        for v in variants:
            avail = bool(v.get("available", False))
            if not avail and not INCLUDE_OOS:
                continue
            grade_raw = (v.get(f"option{grade_pos}") if grade_pos else "") or ""
            grade = clean_grade(grade_raw) or normalize_condition("Refurbished")
            size = ((v.get(f"option{size_pos}") if size_pos else "")
                    or v.get("option2") or "").strip()
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
                    "warranty_days": months_to_days(warranty_months),
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
            condition=grade, warranty_days=o.get("warranty_days"), url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:35} [{grade:12}] {o['availability']:12} ₹{o['price']:.0f}")

    mark_unseen_out_of_stock(SITE, run_started_at)
    print(f"\nDone. Saved {saved} (variant, grade) offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
