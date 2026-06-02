"""
Refit scraper (refitglobal.com) — Shopify-based store.

No Playwright needed. All data comes from Shopify's products.json API:
  /collections/refurbished-mobiles/products.json?limit=250&page=N

Each product has a variants array with:
  option1 = Grade  (SuperB / Very Good / Good)
  option2 = Color  (Black / White / Red / ...)
  option3 = Size   (4GB|128GB / 8GB|256GB / ...)
  price   = paise  (divide by 100)
  available = true/false

Strategy:
  - Group variants by (grade, size) — colour doesn't affect identity
  - For each (grade, size) group, keep only available variants
  - Save the LOWEST price across all available colours
  - Skip (grade, size) combos with zero available variants

Run with: python3 refit.py
"""
import re
import time
import requests
from normalize import clean_model, normalize_storage, make_variant_key, parse_size_string, normalize_condition, is_phone, shopify_option_index
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer
from obs import init_sentry, log_error

SITE = "refit"
BASE_URL = "https://refitglobal.com"
API_URL = f"{BASE_URL}/collections/refurbished-mobiles/products.json"
DELAY = 0.5
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def fetch_all_products():
    """Paginate Shopify products.json until we have everything."""
    products = []
    page = 1
    while True:
        r = requests.get(
            API_URL,
            params={"limit": 250, "page": page},
            headers=HEADERS,
            timeout=30,
        )
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



def get_warranty(product):
    """Extract warranty months from product tags or body HTML."""
    tags = product.get("tags", [])
    for tag in tags:
        m = re.search(r"(\d+)\s*month", str(tag), re.I)
        if m:
            return int(m.group(1))
    # Check body_html for warranty mentions
    body = product.get("body_html", "")
    m = re.search(r"(\d+)\s*month[s]?\s*warranty", body, re.I)
    if m:
        return int(m.group(1))
    return 12  # Refit advertises "up to 12 months" as default


def get_image(product):
    """Get the first product image URL."""
    images = product.get("images", [])
    if images:
        src = images[0].get("src", "")
        # Ensure https
        if src.startswith("//"):
            src = "https:" + src
        return src
    return None


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos("refit")
    print("Fetching all products from Refit API...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")

    # best[(variant_key, grade)] = {price, url, image_url, ...}
    best = {}

    for prod in products:
        title = prod.get("title", "").replace(" Refurbished", "").strip()
        handle = prod.get("handle", "")
        url = f"{BASE_URL}/products/{handle}"
        img_url = get_image(prod)
        warranty_months = get_warranty(prod)

        # "Brand Box" listings are a separate product from the standard one with
        # their own grades. We keep them in the SAME list (same variant_key/name,
        # since clean_model strips "Brand Box") but tag the condition so they read
        # as "Brand Box - Good", "Brand Box - Superb", etc. alongside the regular
        # "Good"/"Superb".
        is_brand_box = bool(re.search(r"brand\s*box", title, re.I))

        # Get rating from Judge.me embedded data — not available in products.json
        # Refit uses Judge.me; rating shown in listing HTML but not in API.
        # We'll skip rating for now and leave as None.
        rating = None
        review_count = None

        variants = prod.get("variants", [])
        if not variants:
            continue
        # Skip fully-out-of-stock products UNLESS the monthly OOS catalog pass is on.
        if not INCLUDE_OOS and not any(v.get("available", False) for v in variants):
            continue

        # Resolve which option slot holds grade vs size by NAME (positions vary
        # per store/product). Fall back to Refit's usual layout (grade=option1,
        # size=option3 or option2) when the option name doesn't identify a role.
        opt_idx = shopify_option_index(prod)
        grade_pos = opt_idx.get("grade", 1)
        size_pos = opt_idx.get("size")

        # Group by (grade, size) → in-stock and out-of-stock (price, variant_id)
        # lists, so we can keep the lowest IN-STOCK price (or, in the OOS pass,
        # the lowest OOS price) and link to that exact variant.
        groups = {}  # (grade, size) → {"in": [...], "oos": [...]}
        for v in variants:
            avail = bool(v.get("available", False))
            if not avail and not INCLUDE_OOS:
                continue
            grade = normalize_condition((v.get(f"option{grade_pos}") or "").strip())
            if size_pos:
                size = (v.get(f"option{size_pos}") or "").strip()
            else:
                size = (v.get("option3") or v.get("option2") or "").strip()
            price_paise = v.get("price", 0)
            price = float(price_paise) if price_paise else None
            if not price or not grade or not size:
                continue
            g = groups.setdefault((grade, size), {"in": [], "oos": []})
            (g["in"] if avail else g["oos"]).append((price, v.get("id")))

        if not groups:
            continue

        model = clean_model(title)

        for (grade, size), g in groups.items():
            ram, storage = parse_size_string(size)
            variant_key = make_variant_key(model, storage, ram)
            # Prefer in-stock; fall back to OOS (only populated in the catalog pass).
            if g["in"]:
                lowest_price, variant_id = min(g["in"], key=lambda pv: pv[0])
                availability = "in_stock"
            elif g["oos"]:
                lowest_price, variant_id = min(g["oos"], key=lambda pv: pv[0])
                availability = "out_of_stock"
            else:
                continue
            variant_url = f"{url}?variant={variant_id}" if variant_id else url
            grade_label = f"Brand Box - {grade}" if is_brand_box else grade

            bkey = (variant_key, grade_label)
            if better_offer(availability, lowest_price, best.get(bkey)):
                best[bkey] = {
                    "model": model,
                    "storage": storage,
                    "ram": ram,
                    "variant_key": variant_key,
                    "grade": grade_label,
                    "price": lowest_price,
                    "availability": availability,
                    "url": variant_url,
                    "image_url": img_url,
                    "warranty_months": warranty_months,
                    "rating": rating,
                    "review_count": review_count,
                    "name": f"{model} {storage or ''}".strip(),
                }

        print(f"  {title}: {len(groups)} (grade, size) combos")
        time.sleep(DELAY)

    # Save to Supabase. A phone is in stock if any of its (variant,grade) offers
    # is in stock; OOS-only phones are saved with in_stock=false.
    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    print(f"\nSaving {len(best)} (variant, grade) offers...")
    saved = 0
    for (vkey, grade), o in best.items():
        # Host image on first sighting
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
            condition=grade, rating=o.get("rating"),
            review_count=o.get("review_count"),
            warranty_months=o.get("warranty_months"),
            url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:35} [{grade:12}] {o['availability']:12} ₹{o['price']:.0f}")

    # Phones not seen in this run -> out of stock (guarded against partial runs).
    mark_unseen_out_of_stock(SITE, run_started_at)

    print(f"\nDone. Saved {saved} (variant, grade) offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise