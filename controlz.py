"""
ControlZ scraper — REQUESTS-ONLY (no Playwright).

The product page embeds a complete JSON-LD `Product.offers[]` inside its Next.js
RSC payload (self.__next_f.push). Each Offer is one real variant:
  sku   = "Apple iPhone 13 (256GB) Blue Saver Series (AP13_256_00034)"
  price = "27879"  (INR, string)
  availability = http://schema.org/InStock | OutOfStock
  url   = ...?variant=<id>   (deep-link)
We parse storage + cosmetic grade from the sku, keep the LOWEST in-stock price
per (variant_key, grade), and save one row per (variant_key, condition). This
replaced the old Playwright DOM-clicking, which iterated conditions × storages
as a full matrix and recorded phantom (condition, storage) combos the store
doesn't offer (and duplicated multi-storage phones). The JSON-LD lists only real
variants with their true availability, so no phantoms, no duplicates, no browser.

Run:  python3 controlz.py [--dry]
"""
import re
import json
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from normalize import clean_model, normalize_storage, normalize_ram, make_variant_key, normalize_condition, is_phone
# obs/db are imported LAZILY (db only when actually writing) so `--dry` validates
# with just `requests` + `normalize`, no Supabase/httpx deps needed.
try:
    from obs import init_sentry, log_error
except Exception:  # deps absent in a bare/local env (e.g. running --dry)
    def init_sentry(*a, **k): pass
    def log_error(*a, **k): pass

SITE = "controlz"
LISTING_URL = "https://www.controlz.world/store"
BASE_URL = "https://www.controlz.world"
WORKERS = 8  # concurrent product fetches (requests, I/O-bound)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _payload(text):
    """Join the Next.js RSC chunks into one decoded string."""
    chunks = re.findall(r'self\.__next_f\.push\(\[\d+,(".*?")\]\)', text, re.S)
    return "".join(json.loads(c) for c in chunks if c.startswith('"'))


def get_product_slugs():
    """All phone product slugs from the listing RSC payload (productType=phone)."""
    resp = requests.get(LISTING_URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    payload = _payload(resp.text)
    SKIP = {"power-bank", "powerbank", "charger", "cable", "case", "cover",
            "earphone", "headphone", "adapter", "hub", "stand"}
    seen, products = set(), []
    for m in re.finditer(r'"productType":"phone","slug":"([^"]+)"', payload):
        slug = m.group(1)
        if slug in seen:
            continue
        if slug in SKIP or any(sk in slug for sk in SKIP) or not is_phone("", slug):
            continue
        seen.add(slug)
        products.append(slug)
    return products


def _extract_offers(payload):
    """The main product's JSON-LD offers[] (the first `"offers":[`), bracket-
    balanced. Returns the parsed list, or [] if absent."""
    i = payload.find('"offers":[')
    if i < 0:
        return [], 0, 0
    start = payload.index('[', i)
    depth = 0
    end = None
    for j in range(start, len(payload)):
        c = payload[j]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        return [], start, start
    try:
        return json.loads(payload[start:end]), start, end
    except Exception:
        return [], start, end


def grade_from_sku(sku):
    """Cosmetic grade from the sku; no grade word = the default 'Premium Renewed'."""
    s = sku.lower()
    if "saver series" in s:
        return "Saver Series"
    if "special series" in s:
        return "Special Series"
    if "premium renewed" in s:
        return "Premium Renewed"
    return "Premium Renewed"


def storage_from_sku(sku):
    m = re.search(r"\(\s*(\d+)\s*(gb|tb)\s*\)", sku, re.I)
    return normalize_storage(f"{m.group(1)}{m.group(2)}") if m else None


def fetch_offers(slug):
    """Fetch one product, return (url, model, image_url, rating, review_count, rows)
    where rows = [(condition, storage, price, variant_url)] for IN-STOCK offers,
    or (..., None, ...) on a non-phone / no-offers page."""
    url = f"{BASE_URL}/products/{slug}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    payload = _payload(r.text)
    offers, start, end = _extract_offers(payload)
    if not offers:
        return url, None, None, None, None, []

    # Model = the common sku prefix (everything before the first " (").
    model = clean_model(offers[0].get("sku", "").split(" (")[0].strip())
    if not model or not is_phone(model, slug):
        return url, None, None, None, None, []

    # Rating + image from the same Product JSON-LD block (window around offers).
    win = payload[max(0, start - 3000):end + 1000]
    rating = review_count = None
    mr = re.search(r'"aggregateRating":\{[^{}]*?"ratingValue":"?([\d.]+)"?[^{}]*?"reviewCount":"?(\d+)"?', win)
    if mr:
        try:
            rating, review_count = float(mr.group(1)), int(mr.group(2))
            if not (rating > 0 and review_count > 0):
                rating = review_count = None
        except Exception:
            rating = review_count = None
    mi = re.search(r'"image":\["(https://[^"]+)"', win)
    image_url = mi.group(1) if mi else None

    rows = []
    for o in offers:
        sku = o.get("sku", "")
        if "instock" not in (o.get("availability", "").lower()):
            continue  # only what's actually available
        st = storage_from_sku(sku)
        try:
            price = float(re.sub(r"[^\d.]", "", str(o.get("price", "")))) or None
        except Exception:
            price = None
        if price is None:
            continue
        rows.append((normalize_condition(grade_from_sku(sku)), st, price, o.get("url") or url))
    return url, model, image_url, rating, review_count, rows


def scrape(dry=False):
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    if not dry:
        from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock
        mark_site_oos("controlz")
    slugs = get_product_slugs()
    print(f"Found {len(slugs)} products ({WORKERS} workers).")

    best = {}        # (variant_key, condition) -> candidate (lowest in-stock price)
    read_ok = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_offers, slug): slug for slug in slugs}
        for fut in as_completed(futures):
            slug = futures[fut]
            try:
                url, model, image_url, rating, review_count, rows = fut.result()
            except Exception as e:
                print(f"  {slug}: ERROR {str(e)[:80]}")
                log_error(e, site=SITE, slug=slug)
                continue
            read_ok += 1
            if not model:
                print(f"  {slug}: skipped (no offers / not a phone)")
                continue
            for cond, st, price, vurl in rows:
                storage = st
                ram = None
                vkey = make_variant_key(model, storage, ram)
                key = (vkey, cond)
                cand = {"model": model, "storage": storage, "ram": ram,
                        "variant_key": vkey, "condition": cond, "price": price,
                        "url": vurl, "image_url": image_url,
                        "rating": rating, "review_count": review_count,
                        "name": f"{model} {storage or ''}".strip()}
                if key not in best or price < best[key]["price"]:
                    best[key] = cand
            print(f"  {slug}: {len(rows)} in-stock offers -> {model}")

    if dry:
        print(f"\n[DRY] {len(best)} (variant, condition) offers:")
        for (vkey, cond), o in sorted(best.items()):
            print(f"  {vkey:34} [{cond:16}] ₹{o['price']:.0f}")
        return

    saved = 0
    for (vkey, cond), o in best.items():
        hosted = None
        if o["image_url"]:
            ext = ".jpg"
            if ".png" in o["image_url"].lower(): ext = ".png"
            elif ".webp" in o["image_url"].lower(): ext = ".webp"
            dest = f"{SITE}/{o['variant_key']}{ext}".replace("|", "_")
            hosted = ensure_image(o["image_url"], dest)
        final_image = hosted or o["image_url"]
        pid = save_phone(SITE, o["name"], o["url"], final_image,
                         o["model"], o["storage"], o["ram"], o["variant_key"])
        save_price(pid, o["price"], availability="in_stock", condition=o["condition"],
                   rating=o.get("rating"), review_count=o.get("review_count"))
        saved += 1
        print(f"  saved: {o['name']:28} [{cond:16}] ₹{o['price']:.0f}")

    ratio = (read_ok / len(slugs)) if slugs else 0.0
    run_complete = bool(slugs) and ratio >= 0.7
    print(f"Read OK: {read_ok}/{len(slugs)} ({ratio*100:.0f}%) — run_complete={run_complete}")
    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=run_complete)
    print(f"\nDone. Saved {saved} (variant, condition) offers from {SITE}.")


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    if not dry:
        init_sentry(SITE)
    try:
        scrape(dry=dry)
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
