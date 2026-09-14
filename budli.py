"""
Budli scraper (buy.budli.in) — Shopify-based refurbished/pre-owned phone store.

Requests-only (Shopify products.json, no Playwright):
  /collections/mobile-phones/products.json?limit=250&page=N (paginate to empty)

Budli uses TWO condition conventions (matching its on-site "Condition guide":
Unboxed / Good / Refurb / Usable / Preowned):
  1. OLDER "refurbished" listings bake the grade into the TITLE parenthetical:
       "Apple iPhone 16 Plus (A3290) 5G 128GB Black (Good Condition)"
     -> "Good Condition" -> Good ; "Refurbished"/none -> Unknown Condition.
  2. NEWER "Used …" listings carry NO grade parenthetical; the grade lives in the
     product TAGS instead ("usable", "PreOwned"/"Pre Owned", "unboxed"):
       "Used Realme 8i 64GB 4GB RAM Space Purple"  tags:[…, PreOwned, usable]  -> Usable
     A product may carry both a category tag (PreOwned/Used) and a finer grade
     tag (usable); the finer grade wins (the PDP shows "Usable"), so tags are
     checked best->worst: Unboxed > Usable > Preowned.
So condition = condition_from_product(title, tags): tag grade first (new
listings), then the title parenthetical (older listings). "Functional Issue" in
the title -> product SKIPPED (defective, not listed). "Refurbished" stays the
vague default -> "Unknown Condition" (not comparable across stores).

Storage: from a Storage/“Storgae” variant option when present (one row per
storage), else parsed from the title — RAM ("8GB/12GB RAM") is removed first so
it isn't mistaken for storage, and the largest remaining GB/TB token wins.

Price: Shopify products.json price is rupees. Availability: per-variant
`available`. Deep-link: /products/<handle>?variant=<id>.

Run with: python3 budli.py   (add --dry for a no-DB validation, --oos to include
sold-out variants in the dry run).
"""
import re
import os
import sys
import time
import requests
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone, normalize_ram
from obs import init_sentry, log_error

SITE = "budli"
BASE_URL = "https://buy.budli.in"
API_URL = f"{BASE_URL}/collections/mobile-phones/products.json"
DELAY = 0.4
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}

# Warranty day conversion, inlined (not imported from db) so the parse
# (build_offers / warranty_from_body) stays import-free for --dry.
MONTH_DAYS = 30
YEAR_DAYS = 365

# Budli's on-site "Condition guide" grades that live in the product TAGS (newer
# "Used …" listings). Ordered best -> worst; a product carrying several (e.g.
# category "PreOwned" + grade "usable") takes the FINER grade, matching the PDP.
TAG_GRADES = [
    ("unboxed", "Unboxed"),
    ("usable", "Usable"),
    ("preowned", "Preowned"),
    ("pre owned", "Preowned"),
]


def condition_from_product(title, tags):
    """Return the condition label, or None to SKIP (Functional Issue).

    Grade sources, in order:
      1. TAG grade (newer "Used …" listings) — Unboxed / Usable / Preowned.
      2. TITLE parenthetical (older listings) — Good / Unboxed / Refurbished.
    "Functional Issue" anywhere in the title -> skip. Anything else / nothing
    -> "Unknown Condition" (the vague "Refurbished" default)."""
    parens = [c.strip().lower() for c in re.findall(r"\(([^)]*)\)", title or "")]
    for cl in parens:
        if "functional issue" in cl or "functinal issue" in cl:
            return None  # defective — skip

    tagset = {(t or "").strip().lower() for t in (tags or [])}
    for tag, label in TAG_GRADES:
        if tag in tagset:
            return label

    for cl in parens:
        if "good" in cl:
            return normalize_condition("Good")
        if "unboxed" in cl:
            return "Unboxed"
        if "refurbish" in cl:
            return normalize_condition("Refurbished")  # -> Unknown Condition
    return normalize_condition("Refurbished")  # no/other paren -> Unknown Condition


def warranty_from_body(body):
    """Return (warranty_days, warranty_label) from the product body:
      "6 months Budli service warranty"  → (180, None)
      "1 year Budli service warranty"     → (365, None)
      "Brand warranty till 13-May-2027"   → (None, "Brand Warranty")
      "No warranty"                       → (0, None)  (explicitly none)
    (None, None) when nothing is stated. A manufacturer/brand warranty is shown
    as "Brand Warranty" (its remaining duration isn't a Budli-backed promise)."""
    if not body:
        return None, None
    s = re.sub(r"<[^>]+>", " ", body).lower()
    m = re.search(r"(\d+)\s*(year|month)s?\b[^.<\n]{0,25}warrant", s)
    if m:
        n = int(m.group(1))
        return (n * YEAR_DAYS if m.group(2) == "year" else n * MONTH_DAYS), None
    if "brand warranty" in s:
        return None, "Brand Warranty"
    if "no warranty" in s:
        return 0, None
    return None, None


def storage_opt_pos(prod):
    """1-based position of the Storage option (handles the 'Storgae' typo), or None."""
    for o in prod.get("options", []):
        n = (o.get("name") or "").strip().lower()
        if "stor" in n or "size" in n or "capacity" in n:
            return o.get("position")
    return None


def ram_from_text(text):
    """A single, unambiguous labelled RAM ("12 GB RAM" -> 12GB) from any text. A
    SLASH RANGE ("8GB/12GB RAM") is ambiguous -> None. normalize_ram requires the
    word RAM, so a bare storage token is never misread."""
    if re.search(r"\d+\s*GB\s*/\s*\d+\s*GB\s*RAM", text or "", re.I):
        return None
    return normalize_ram(text or "")


def ram_from_title(title):
    """Backwards-compatible alias — RAM from the product title."""
    return ram_from_text(title)


def ram_from_body(body_html):
    """RAM from the product BODY (Budli's Specifications block reliably carries
    "Memory: 12 GB RAM" on every listing, even when the title omits it or a
    cheaper colliding listing wins the per-(variant,condition) dedup). HTML is
    stripped first."""
    return ram_from_text(re.sub(r"<[^>]+>", " ", body_html or ""))


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


def better_offer(new_availability, new_price, cur):
    """Pure copy of db.better_offer (kept db-free so build_offers runs under
    --dry): in_stock beats out_of_stock; within the same availability the lower
    price wins. `cur` is the current offer dict or None."""
    if cur is None:
        return True
    new_in = new_availability == "in_stock"
    cur_in = cur.get("availability") == "in_stock"
    if new_in != cur_in:
        return new_in
    return new_price < cur["price"]


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


def build_offers(products, include_oos):
    """Parse products -> {(variant_key, condition): lowest-price offer}. Pure
    (no DB), so it drives both scrape() and the --dry validator."""
    best = {}
    for prod in products:
        title = prod.get("title", "")
        model = clean_model(title)
        if not model or not is_phone(model, title):
            continue

        condition = condition_from_product(title, prod.get("tags"))
        if condition is None:
            continue  # Functional Issue — skip

        handle = prod.get("handle", "")
        url = f"{BASE_URL}/products/{handle}"
        img_url = get_image(prod)
        warranty_days, warranty_label = warranty_from_body(prod.get("body_html", ""))
        title_storage = storage_from_title(title)
        title_ram = ram_from_title(title) or ram_from_body(prod.get("body_html", ""))
        spos = storage_opt_pos(prod)

        variants = prod.get("variants", [])
        if not variants:
            continue
        if not include_oos and not any(v.get("available", False) for v in variants):
            continue

        for v in variants:
            avail = bool(v.get("available", False))
            if not avail and not include_oos:
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
                    "model": model, "storage": storage, "ram": title_ram,
                    "variant_key": variant_key, "condition": condition,
                    "price": price, "availability": availability,
                    "url": variant_url, "image_url": img_url,
                    "warranty_days": warranty_days,
                    "warranty_label": warranty_label,
                    "name": f"{model} {storage}".strip(),
                }
    return best


def scrape():
    from datetime import datetime, timezone
    from db import (save_phone, save_price, ensure_image, mark_site_oos,
                    mark_unseen_out_of_stock, INCLUDE_OOS)
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)
    print("Fetching all products from Budli API...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")

    best = build_offers(products, INCLUDE_OOS)
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
            condition=o["condition"], warranty_days=o.get("warranty_days"),
            warranty_label=o.get("warranty_label"), url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:38} [{o['condition']:18}] ₹{o['price']:.0f}  [{o['availability']}]")

    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=bool(best))
    print(f"\nDone. Saved {saved} offers from {SITE}.")


def dry(include_oos):
    """No-DB validation: fetch, parse, print the condition distribution + offers."""
    from collections import Counter
    print("Fetching all products from Budli API...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")
    best = build_offers(products, include_oos)
    dist = Counter(o["condition"] for o in best.values())
    print(f"\nUnique (variant, condition) offers: {len(best)}")
    print("Condition distribution:")
    for c, n in dist.most_common():
        print(f"  {n:4}  {c}")
    print()
    for o in sorted(best.values(), key=lambda x: (x["model"], x["condition"])):
        print(f"  {o['name']:40} [{o['condition']:18}] ₹{o['price']:.0f}  ram={o['ram']}  [{o['availability']}]")


if __name__ == "__main__":
    dry_run = "--dry" in sys.argv
    include_oos = ("--oos" in sys.argv) or (os.environ.get("INCLUDE_OOS") == "1")
    if dry_run:
        dry(include_oos)
    else:
        init_sentry(SITE)
        try:
            scrape()
        except Exception as e:
            log_error(e, site=SITE, phase="scrape")
            raise
