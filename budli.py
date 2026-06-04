"""
Budli scraper (buy.budli.in) — Shopify-based refurbished/pre-owned phone store.

Requests-only (Shopify products.json, no Playwright):
  /collections/mobile-phones/products.json?limit=250&page=N (paginate to empty)

Unlike the other Shopify stores, Budli bakes model + storage + colour + CONDITION
into the product TITLE, and ~90% of products are single-variant ("Default Title"):
  "Apple iPhone 16 Plus (A3290) 5G 128GB Black (Good Condition)"
So model/storage/condition are parsed from the title (clean_model strips the
parens/colour/5G/storage); the trailing parenthetical carries the condition.

Condition mapping (per store owner):
  - "Good Condition"            -> "Good"
  - "Refurbished"               -> "Unknown Condition" (the vague default)
  - "Functional Issue"          -> product SKIPPED (defective; not listed)
  - "Unboxed - Brand Warranty"  -> kept as-is
  - no/other parenthetical      -> "Unknown Condition"

Storage: from a Storage/“Storgae” variant option when present (one row per
storage), else parsed from the title — RAM ("8GB/12GB RAM") is removed first so
it isn't mistaken for storage, and the largest remaining GB/TB token wins.

Price: Shopify products.json price is rupees. Availability: per-variant
`available`. Deep-link: /products/<handle>?variant=<id>.

Run with: python3 budli.py
"""
import re
import time
import requests
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer
from obs import init_sentry, log_error

SITE = "budli"
BASE_URL = "https://buy.budli.in"
API_URL = f"{BASE_URL}/collections/mobile-phones/products.json"
DELAY = 0.4
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def condition_from_title(title):
    """Return the condition label, or None to SKIP (Functional Issue)."""
    for c in re.findall(r"\(([^)]*)\)", title):
        cl = c.strip().lower()
        if "functional issue" in cl or "functinal issue" in cl:
            return None  # defective — skip
        if "good" in cl:
            return normalize_condition("Good")
        if "unboxed" in cl:
            return "Unboxed - Brand Warranty"
        if "refurbish" in cl:
            return normalize_condition("Refurbished")  # -> Unknown Condition
    return normalize_condition("Refurbished")  # no/other paren -> Unknown Condition


def storage_opt_pos(prod):
    """1-based position of the Storage option (handles the 'Storgae' typo), or None."""
    for o in prod.get("options", []):
        n = (o.get("name") or "").strip().lower()
        if "stor" in n or "size" in n or "capacity" in n:
            return o.get("position")
    return None


def storage_from_title(title):
    """Largest GB/TB token in the title, after dropping the RAM spec so it isn't
    read as storage ('256GB 8GB/12GB RAM' -> 256GB)."""
    t = re.sub(r"\([^)]*\)", " ", title)
    t = re.sub(r"(?:\d+\s*GB\s*/\s*)?\d+\s*GB\s*RAM", " ", t, flags=re.I)
    toks = re.findall(r"(\d+)\s*(GB|TB)", t, re.I)
    if not toks:
        return None
    def gb(p):
        n, u = p
        return int(n) * (1024 if u.upper() == "TB" else 1)
    n, u = max(toks, key=gb)
    return normalize_storage(f"{n}{u.upper()}")


def get_image(product):
    images = product.get("images", [])
    if images:
        src = images[0].get("src", "")
        if src.startswith("//"):
            src = "https:" + src
        return src or None
    return None


def fetch_all_products():
    """Paginate until an empty page."""
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
    print("Fetching all products from Budli API...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")

    best = {}  # (variant_key, condition) -> lowest-price offer

    for prod in products:
        title = prod.get("title", "")
        model = clean_model(title)
        if not model or not is_phone(model, title):
            continue

        condition = condition_from_title(title)
        if condition is None:
            continue  # Functional Issue — skip

        handle = prod.get("handle", "")
        url = f"{BASE_URL}/products/{handle}"
        img_url = get_image(prod)
        title_storage = storage_from_title(title)
        spos = storage_opt_pos(prod)

        variants = prod.get("variants", [])
        if not variants:
            continue
        if not INCLUDE_OOS and not any(v.get("available", False) for v in variants):
            continue

        for v in variants:
            avail = bool(v.get("available", False))
            if not avail and not INCLUDE_OOS:
                continue
            price = float(v.get("price")) if v.get("price") else None
            if not price:
                continue
            storage = (normalize_storage(v.get(f"option{spos}")) if spos else None) or title_storage
            if not storage:
                continue

            variant_key = make_variant_key(model, storage, None)
            availability = "in_stock" if avail else "out_of_stock"
            variant_id = v.get("id")
            variant_url = f"{url}?variant={variant_id}" if variant_id else url

            bkey = (variant_key, condition)
            if better_offer(availability, price, best.get(bkey)):
                best[bkey] = {
                    "model": model, "storage": storage, "ram": None,
                    "variant_key": variant_key, "condition": condition,
                    "price": price, "availability": availability,
                    "url": variant_url, "image_url": img_url,
                    "name": f"{model} {storage}".strip(),
                }

    print(f"\nUnique (variant, condition) offers: {len(best)}")

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
        )
        saved += 1
        print(f"  saved: {o['name']:38} [{o['condition']:18}] ₹{o['price']:.0f}  [{o['availability']}]")

    mark_unseen_out_of_stock(SITE, run_started_at)
    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
