"""
ControlZ scraper — REQUESTS-ONLY (no Playwright).

The product page embeds the full variant matrix in its Next.js RSC payload
(self.__next_f.push). We use two parts of it:
  - the JSON-LD `Product.offers[]` to identify THIS product's model (the common
    sku prefix — proven to be the main product, not a related one), and
  - the `variants[]` objects: each has sku, price, `inventory`, and `options`
    (CATEGORY grade / SIZE storage / COLOR). `inventory > 0` is the real
    availability (matches the rendered Add-to-Cart, like Cashify's
    availableInventory) — the JSON-LD `availability` flag and the `status`
    field are NOT reliable (status is mostly DRAFT even when sellable).

Per (variant_key, cosmetic grade) we keep the LOWEST in-stock price. This
replaced the Playwright DOM-clicking, which iterated conditions × storages as a
full matrix and recorded phantom combos the store doesn't offer + duplicated
multi-storage phones. No browser, no phantoms, exact per-variant prices.

Grade comes from the sku text (Saver Series / Special Series / else Premium
Renewed) since the CATEGORY optionId is sometimes an unmapped code.

Run:  python3 controlz.py [--dry]
"""
import re
import json
import sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone
# obs/db imported LAZILY (db only when writing) so `--dry` runs with just requests.
try:
    from obs import init_sentry, log_error
except Exception:
    def init_sentry(*a, **k): pass
    def log_error(*a, **k): pass

SITE = "controlz"
LISTING_URL = "https://www.controlz.world/store"
BASE_URL = "https://www.controlz.world"
WORKERS = 8
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _payload(text):
    chunks = re.findall(r'self\.__next_f\.push\(\[\d+,(".*?")\]\)', text, re.S)
    return "".join(json.loads(c) for c in chunks if c.startswith('"'))


def _balanced_array(payload, key, frm=0):
    """Return (parsed_list, end_index) for the first `"<key>":[ ... ]` at/after
    frm, bracket-balanced. (None, frm) if absent/unparseable."""
    i = payload.find(f'"{key}":[', frm)
    if i < 0:
        return None, frm
    s = payload.index('[', i)
    depth = 0
    for j in range(s, len(payload)):
        if payload[j] == '[':
            depth += 1
        elif payload[j] == ']':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(payload[s:j + 1]), j + 1
                except Exception:
                    return None, j + 1
    return None, len(payload)


def _all_variants(payload):
    """Every inline variants[] object across the payload (the page carries the
    main product + related products; we filter by model afterwards)."""
    out, idx = [], 0
    while True:
        arr, end = _balanced_array(payload, "variants", idx)
        if end <= idx:
            break
        if isinstance(arr, list):
            out.extend(v for v in arr if isinstance(v, dict))
        idx = end
    return out


def get_product_slugs():
    resp = requests.get(LISTING_URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    payload = _payload(resp.text)
    SKIP = {"power-bank", "powerbank", "charger", "cable", "case", "cover",
            "earphone", "headphone", "adapter", "hub", "stand"}
    seen, slugs = set(), []
    for m in re.finditer(r'"productType":"phone","slug":"([^"]+)"', payload):
        slug = m.group(1)
        if slug in seen:
            continue
        if slug in SKIP or any(sk in slug for sk in SKIP) or not is_phone("", slug):
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def grade_from_sku(sku):
    s = sku.lower()
    if "saver series" in s:
        return "Saver Series"
    if "special series" in s:
        return "Special Series"
    if "premium renewed" in s:
        return "Premium Renewed"
    return "Premium Renewed"   # default base grade (no grade word in sku)


def storage_from_variant(v):
    # Prefer the SIZE option ("128-GB"); fall back to the sku.
    for o in v.get("options") or []:
        if o.get("groupId") == "SIZE" and o.get("optionId"):
            st = normalize_storage(o["optionId"])
            if st:
                return st
    m = re.search(r"\(\s*(\d+)\s*(gb|tb)\s*\)", v.get("sku") or "", re.I)
    return normalize_storage(f"{m.group(1)}{m.group(2)}") if m else None


def fetch_offers(slug):
    """(url, model, image, rating, review_count, rows) — rows = [(condition,
    storage, price, url)] for IN-STOCK (inventory>0) variants of THIS product."""
    url = f"{BASE_URL}/products/{slug}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    payload = _payload(r.text)

    # Model = the main product's JSON-LD offers sku prefix (before the first " (").
    offers, oend = _balanced_array(payload, "offers")
    if not offers:
        return url, None, None, None, None, []
    model = clean_model((offers[0].get("sku") or "").split(" (")[0].strip())
    if not model or not is_phone(model, slug):
        return url, None, None, None, None, []
    prefix = (offers[0].get("sku") or "").split(" (")[0].strip()  # e.g. "Apple iPhone 13"

    # rating + image from the Product JSON-LD block (window around offers).
    win = payload[max(0, payload.find('"offers":[') - 3000):oend + 1000]
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
    for v in _all_variants(payload):
        sku = v.get("sku") or ""
        # Exact model match: "<prefix> (" excludes "13 Pro"/related products.
        if not sku.startswith(prefix + " ("):
            continue
        try:
            inv = int(v.get("inventory") or 0)
        except Exception:
            inv = 0
        if inv <= 0:
            continue   # inventory>0 = the real, rendered availability
        st = storage_from_variant(v)
        try:
            price = float(v.get("price"))
        except Exception:
            continue
        rows.append((normalize_condition(grade_from_sku(sku)), st, price, url))
    return url, model, image_url, rating, review_count, rows


def scrape(dry=False):
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    if not dry:
        from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock
        mark_site_oos("controlz")
    slugs = get_product_slugs()
    print(f"Found {len(slugs)} products ({WORKERS} workers).")

    best, read_ok = {}, 0
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
                vkey = make_variant_key(model, st, None)
                key = (vkey, cond)
                cand = {"model": model, "storage": st, "ram": None,
                        "variant_key": vkey, "condition": cond, "price": price,
                        "url": vurl, "image_url": image_url,
                        "rating": rating, "review_count": review_count,
                        "name": f"{model} {st or ''}".strip()}
                if key not in best or price < best[key]["price"]:
                    best[key] = cand
            print(f"  {slug}: {len(rows)} in-stock variants -> {model}")

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
