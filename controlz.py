"""
ControlZ scraper (controlz.world) — Shopify products.json (requests-only).

MIGRATION (2026-07): ControlZ moved off its old custom Next.js/RSC storefront
(which required a Playwright DOM scrape) onto SHOPIFY. The old site is gone:
`www.controlz.world/store` now 301-redirects to `/collections/store`, and the
whole catalog is available as standard Shopify product JSON at
`/collections/store/products.json`. So the scraper is now a plain requests-only
Shopify reader (no browser, no ThreadPoolExecutor) like refit/tetro/grest.

Structure of a ControlZ product:
  options: Category (the GRADE: "Premium Renewed" / "Saver Series") × Storage ×
           Color, and on some products a SIM axis ("E-sim"). Option slot order
           varies per product, so Storage is resolved by NAME via
           shopify_option_index and the grade is resolved by NAME ("Category").
  variants: option1/2/3 + integer-rupee `price` (string "15999.00") + `available`.

Availability = the per-variant Shopify `available` flag (the buyable state the
storefront renders — the validated rule for Shopify stores).

We save ONE row per (variant_key, condition) at the LOWEST price across colours
(and SIM). Grades map through normalize_condition; a product with NO Category
axis falls back to ControlZ's primary grade "Premium Renewed". Storage-less
products (accessories) are skipped, and is_phone() drops non-phones.

Warranty: ControlZ advertises one blanket warranty, curated as the store-level
`stores.default_warranty_days` (540), so it is NOT set per offer. Reviews are no
longer available (the old DOM header exposed "4.7 · 21 REVIEWS"; Shopify
products.json carries none), so ratings are left null.

Deep-link: /products/<handle>?variant=<id>. OOS-capable (INCLUDE_OOS=1 saves
sold-out variants at their lowest selling price for the SEO catalog).

Run:  python3 controlz.py            (live scrape + DB write)
      python3 controlz.py --dry      (fetch + parse + print, NO DB / no deps)
      INCLUDE_OOS=1 python3 controlz.py   (also save sold-out variants)

db/obs are imported LAZILY inside scrape()/__main__ so the pure parsing
(fetch + build_offers) runs with only `requests` + `normalize` installed.
"""
import re
import sys
import time
import requests
from normalize import (
    clean_model, normalize_storage, make_variant_key,
    normalize_condition, is_phone, shopify_option_index,
)

SITE = "controlz"
BASE_URL = "https://www.controlz.world"
# Root products.json = the FULL catalog (the /collections/store subset misses
# ~a third of the phones). Paginated until an empty page below.
API_URL = f"{BASE_URL}/products.json"
DELAY = 0.4
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}
# ControlZ grades: "Premium Renewed" / "Saver Series". When a product exposes no
# Category (grade) axis, fall back to the store's primary grade.
DEFAULT_CONDITION = "Premium Renewed"

# Use the canonical better_offer when the DB stack is importable; fall back to an
# identical pure copy so `--dry` (and this parse) run with only requests+normalize.
try:
    from db import better_offer  # noqa: F401
except Exception:
    def better_offer(new_availability, new_price, cur):
        if cur is None:
            return True
        new_in = new_availability == "in_stock"
        cur_in = cur.get("availability") == "in_stock"
        if new_in != cur_in:
            return new_in
        return new_price < cur["price"]


def _gb(tok):
    m = re.search(r"([\d.]+)\s*(TB|GB)\b", tok, re.I)
    if not m:
        return None
    n = float(m.group(1))
    return n * 1024 if m.group(2).upper() == "TB" else n


def split_ram_storage(raw):
    """Some ControlZ Storage options bundle RAM as "8GB/256GB" (RAM/Storage).
    Return (storage_token, ram_token): the LARGER capacity is storage, the
    smaller GB token is RAM. A plain "256GB" returns ("256GB", None).
    (Mirrors the itradeit/oldsold RAM-bundled-in-storage handling.)"""
    parts = [p.strip() for p in str(raw).split("/") if p.strip()]
    if len(parts) <= 1:
        return (parts[0] if parts else ""), None
    sized = [(g, p) for p in parts if (g := _gb(p)) is not None]
    if len(sized) < 2:
        return parts[-1], None
    sized.sort()
    return sized[-1][1], sized[0][1]  # storage=largest, ram=smallest


def get_image(product):
    images = product.get("images", []) or []
    if images:
        src = images[0].get("src", "") or ""
        if src.startswith("//"):
            src = "https:" + src
        return src or None
    return None


def grade_position(product):
    """1-based option index of the grade axis, or None.

    shopify_option_index catches option names containing grade/condition/quality;
    ControlZ names theirs "Category", so check that explicitly too.
    """
    pos = shopify_option_index(product).get("grade")
    if pos:
        return pos
    for opt in product.get("options", []) or []:
        name = (opt.get("name") or "").strip().lower()
        if any(k in name for k in ("category", "grade", "condition", "quality")):
            return opt.get("position")
    return None


def fetch_all_products():
    """Page through the collection products.json until an empty page.

    A collection endpoint can cap the page size BELOW the requested limit, so we
    stop on an EMPTY batch (not on `len(batch) < limit`) to avoid truncating.
    """
    products, page = [], 1
    while True:
        try:
            r = requests.get(API_URL, params={"limit": 250, "page": page},
                             headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"  request error at page {page}: {e}")
            break
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
        if page > 60:  # safety stop
            break
    return products


def build_offers(products, include_oos=False):
    """Pure parse: products -> {(variant_key, condition): offer}. No DB."""
    best = {}  # (variant_key, condition) -> lowest-price offer
    for prod in products:
        title = prod.get("title", "") or ""
        model = clean_model(title)
        if not model or not is_phone(model, prod.get("handle", "")):
            continue

        handle = prod.get("handle", "")
        url = f"{BASE_URL}/products/{handle}"
        img_url = get_image(prod)

        variants = prod.get("variants", []) or []
        if not variants:
            continue
        if not include_oos and not any(v.get("available", False) for v in variants):
            continue

        size_pos = shopify_option_index(prod).get("size")
        if not size_pos:
            continue  # no Storage option -> can't key reliably
        gpos = grade_position(prod)

        for v in variants:
            avail = bool(v.get("available", False))
            if not avail and not include_oos:
                continue
            storage_raw, ram_raw = split_ram_storage((v.get(f"option{size_pos}") or "").strip())
            storage = normalize_storage(storage_raw)
            if not storage:
                continue
            ram = ram_raw or None  # web normalizes RAM; store the token as-is
            try:
                price = float(v.get("price")) if v.get("price") else None
            except (TypeError, ValueError):
                price = None
            if not price:
                continue

            grade_raw = (v.get(f"option{gpos}") or "").strip() if gpos else ""
            condition = normalize_condition(grade_raw) or DEFAULT_CONDITION

            variant_key = make_variant_key(model, storage, None)  # storage-only
            availability = "in_stock" if avail else "out_of_stock"
            variant_id = v.get("id")
            variant_url = f"{url}?variant={variant_id}" if variant_id else url

            # RAM in the key too, so a rare same-storage/different-RAM listing
            # stays a distinct offer (matches oldsold/itradeit).
            bkey = (variant_key, condition, ram)
            name = f"{model} {storage}" + (f" {ram}" if ram else "")
            if better_offer(availability, price, best.get(bkey)):
                best[bkey] = {
                    "model": model, "storage": storage, "ram": ram,
                    "variant_key": variant_key, "condition": condition,
                    "price": price, "availability": availability,
                    "url": variant_url, "image_url": img_url,
                    "name": name.strip(),
                }
    return best


def scrape():
    from datetime import datetime, timezone
    from db import (save_phone, save_price, ensure_image, mark_site_oos,
                    mark_unseen_out_of_stock, INCLUDE_OOS)

    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)
    print("Fetching all products from ControlZ (Shopify products.json)...")
    products = fetch_all_products()
    print(f"\nTotal products: {len(products)}")

    best = build_offers(products, include_oos=INCLUDE_OOS)

    # A phone (site+name) is in stock iff ANY of its condition offers is in stock.
    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}

    print(f"\nSaving {len(best)} (variant, condition) offers...")
    saved = 0
    for (vkey, cond, _ram), o in best.items():
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
            condition=cond, url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:32} [{cond:16}] {o['availability']:12} ₹{o['price']:.0f}")

    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=bool(best))
    print(f"\nDone. Saved {saved} offers from {SITE}.")


def _dry():
    """Fetch + parse + print, NO DB (works with just requests + normalize)."""
    include_oos = "--oos" in sys.argv
    products = fetch_all_products()
    print(f"\nTotal products fetched: {len(products)}")
    best = build_offers(products, include_oos=include_oos)
    from collections import Counter
    conds = Counter(c for (_vk, c, _r) in best.keys())
    instock = sum(1 for o in best.values() if o["availability"] == "in_stock")
    print(f"\n{len(best)} (variant, condition) offers | {instock} in stock | conditions: {dict(conds)}\n")
    for o in sorted(best.values(), key=lambda x: (x["name"], x["condition"])):
        ram = f" ram={o['ram']}" if o["ram"] else ""
        print(f"  {o['name']:36} [{o['condition']:16}] {o['availability']:12} ₹{o['price']:.0f}  {o['variant_key']}{ram}")


if __name__ == "__main__":
    if "--dry" in sys.argv:
        _dry()
    else:
        from obs import init_sentry, log_error
        init_sentry(SITE)
        try:
            scrape()
        except Exception as e:
            log_error(e, site=SITE, phase="scrape")
            raise
