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
from normalize import clean_model, normalize_storage, make_variant_key, parse_size_string, normalize_condition
from db import save_phone, save_price, ensure_image

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

        # Get rating from Judge.me embedded data — not available in products.json
        # Refit uses Judge.me; rating shown in listing HTML but not in API.
        # We'll skip rating for now and leave as None.
        rating = None
        review_count = None

        variants = prod.get("variants", [])
        if not variants:
            continue
        # Skip entirely if no variant is available (fully out of stock)
        if not any(v.get("available", False) for v in variants):
            continue

        # Group by (grade, size) → collect available prices
        groups = {}  # (grade, size) → list of prices for available colors
        for v in variants:
            if not v.get("available", False):
                continue
            grade = normalize_condition((v.get("option1") or "").strip())
            size = (v.get("option3") or v.get("option2") or "").strip()
            price_paise = v.get("price", 0)
            price = float(price_paise) if price_paise else None
            if not price or not grade or not size:
                continue
            key = (grade, size)
            groups.setdefault(key, []).append(price)

        if not groups:
            # No available variants — skip entirely
            continue

        model = clean_model(title)

        for (grade, size), prices in groups.items():
            ram, storage = parse_size_string(size)
            variant_key = make_variant_key(model, storage, ram)
            lowest_price = min(prices)

            bkey = (variant_key, grade)
            if bkey not in best or lowest_price < best[bkey]["price"]:
                best[bkey] = {
                    "model": model,
                    "storage": storage,
                    "ram": ram,
                    "variant_key": variant_key,
                    "grade": grade,
                    "price": lowest_price,
                    "url": url,
                    "image_url": img_url,
                    "warranty_months": warranty_months,
                    "rating": rating,
                    "review_count": review_count,
                    "name": f"{model} {storage or ''}".strip(),
                }

        print(f"  {title}: {len(groups)} available (grade, size) combos")
        time.sleep(DELAY)

    # Save to Supabase
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
            o["model"], o["storage"], o["ram"], o["variant_key"]
        )
        save_price(
            pid, o["price"], availability="in_stock",
            condition=grade, rating=o.get("rating"),
            review_count=o.get("review_count"),
            warranty_months=o.get("warranty_months"),
        )
        saved += 1
        print(f"  saved: {o['name']:35} [{grade:12}] ₹{o['price']:.0f}")

    print(f"\nDone. Saved {saved} (variant, grade) offers from {SITE}.")


if __name__ == "__main__":
    scrape()