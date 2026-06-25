"""
Ovantica scraper (ovantica.com) — refurbished smartphones.

Two-phase approach:
  Phase 1: Playwright listing page (one session) — click Load More, intercept
           _rsc= URLs to collect every product URL.
  Phase 2: Plain requests per product page (thread-pooled) — parse the RSC
           payload for full variant data.

Availability: each variant in the RSC carries a "stock_update" field
("in_stock"/"out_of_stock") that matches the rendered Add-to-Cart button.
We filter on that (NOT on "qty", which stays >0 even for sold-out variants —
phantom inventory). This lets us avoid opening a browser per product.

Groups by (model, storage, condition), keeps the lowest price across colors,
and deep-links to the chosen variant via its id (the slug's trailing number).

Run with: python3 ovantica.py
"""
import re
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer
from obs import init_sentry, log_error

SITE = "ovantica"
BASE_URL = "https://ovantica.com"
LISTING_URL = f"{BASE_URL}/buy-refurbished-smartphones"
CDN = "https://cdn.ovantica.com/cdn-cgi/image/width=400,quality=80,format=auto/images/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}
WORKERS = 10  # parallel product fetches


def get_product_urls():
    """Use Playwright to click Load More and collect all product URLs via _rsc= intercept."""
    print("Loading listing page (clicking Load More)...")
    product_urls = {}  # slug (no variant id) -> full path

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})

        def on_response(resp):
            m = re.search(r"(/buy-refurbished[^?]+/(\d+))\?_rsc=", resp.url)
            if m:
                path = m.group(1)
                # The trailing number is a per-VARIANT id; the listing (and Next.js
                # prefetch) emits one URL per color/variant, so the SAME phone shows
                # up under several ids. Key by the slug WITHOUT the trailing id so we
                # fetch each phone once — otherwise we scrape it repeatedly and emit
                # duplicate condition rows. The payload on any one variant URL carries
                # every variant, so the specific entry id we keep doesn't matter.
                slug = re.sub(r"/\d+$", "", path)
                if slug not in product_urls:
                    product_urls[slug] = path

        page.on("response", on_response)
        page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        clicks = 0
        while True:
            try:
                btn = page.locator("button:has-text('Load more')").first
                if not btn.is_visible(timeout=3000):
                    break
                btn.click()
                page.wait_for_timeout(1500)
                clicks += 1
            except Exception:
                break

        print(f"  {clicks} Load More clicks → {len(product_urls)} product URLs")
        browser.close()

    return product_urls


def extract_variant_objects(payload, key='"condition"'):
    """Return the raw JSON text of each variant object in the RSC payload.
    Anchored on the condition key, brace-matched with string-awareness (so braces
    inside string values don't throw off the matching)."""
    out, i, n = [], 0, len(payload)
    while True:
        p = payload.find(key, i)
        if p == -1:
            break
        st = payload.rfind('{', 0, p)
        depth = 0
        instr = False
        esc = False
        end = None
        for k in range(st, n):
            c = payload[k]
            if instr:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    instr = False
            else:
                if c == '"':
                    instr = True
                elif c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        end = k + 1
                        break
        if end:
            out.append(payload[st:end])
            i = end
        else:
            i = p + 1
    return out


def ram_from_name(name):
    """Ovantica bakes "(RAM, STORAGE)" into the product name, e.g.
    "Buy Oppo A96 (8GB, 128GB) Black - Renewed" -> 8GB RAM. The two GB tokens in
    the parenthetical are (RAM, storage); the SMALLER is the RAM. Returns None if
    there's no such pair (or both look like storage, i.e. min > 24GB) — the RSC
    variant object itself has no RAM field, this name string is the only signal."""
    m = re.search(r"\(\s*(\d+)\s*GB\s*,\s*(\d+)\s*GB\s*\)", name or "", re.I)
    if not m:
        return None
    ram = min(int(m.group(1)), int(m.group(2)))
    return f"{ram}GB" if ram <= 24 else None


def _variant_image(v):
    """Resolve a variant's image (the RSC stores it as a JSON array string)."""
    raw = v.get("image")
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, list) and raw:
            return CDN + raw[0]
    except Exception:
        pass
    return None


def parse_product_page(path):
    """Fetch the product page over plain HTTP (no browser) and parse its RSC
    payload. Availability = each variant's stock_update == "in_stock" (validated
    to match the rendered Add-to-Cart). Keeps the lowest in-stock price per
    (condition, storage) across colors and deep-links to that variant's id."""
    url = BASE_URL + path
    r = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(10, 20))
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                return None        # read failure (distinct from a parsed-but-empty page)
            time.sleep(2)
        except Exception:
            return None
    if not r or r.status_code != 200:
        return None

    chunks = re.findall(r'self\.__next_f\.push\(\[\d+,(".*?")\]\)', r.text, re.S)
    payload = "".join(json.loads(c) for c in chunks if c.startswith('"'))

    variants = []
    for o in extract_variant_objects(payload):
        if '"stock_update"' not in o or '"sku"' not in o or '"storage"' not in o:
            continue
        try:
            v = json.loads(o)
        except Exception:
            continue
        if "condition" in v and "price" in v:
            variants.append(v)
    if not variants:
        return None        # 200 but no RSC variant matrix -> treat as a block/failure

    model = clean_model(variants[0].get("name", "") or "")
    if not model or not is_phone(model):
        return []          # read OK, just filtered out

    # Rating/reviews from the payload's schema aggregateRating.
    rating, review_count = None, None
    rm = re.search(r'"ratingValue":"([^"]+)".*?"reviewCount":"([^"]+)"', payload)
    if rm:
        try:
            rating = float(rm.group(1))
            review_count = int(rm.group(2))
        except (ValueError, TypeError):
            pass

    base_slug = re.sub(r"/\d+/?$", "", path)

    # Lowest IN-STOCK price per (condition, storage) across colors; keep that
    # variant's id for the deep link. stock_update is the availability source of
    # truth (qty is unreliable — stays >0 on sold-out variants). In the monthly
    # OOS catalog pass (INCLUDE_OOS) we also keep out_of_stock variants.
    best = {}  # (cond_lower, storage) -> {price, storage, condition, vid, image, availability}
    for v in variants:
        avail = (v.get("stock_update") or "").lower() == "in_stock"
        if not avail and not INCLUDE_OOS:
            continue
        condition = v.get("condition") or ""
        if re.sub(r"[^a-z]", "", condition.lower()) == "asis":
            continue  # skip the lowest "As-Is" grade — we don't list these
        storage = v.get("storage")
        price = float(v.get("price") or 0)
        if not storage or not price:
            continue
        key = (condition.lower(), storage)
        availability = "in_stock" if avail else "out_of_stock"
        if better_offer(availability, price, best.get(key)):
            best[key] = {
                "price": price, "storage": storage, "condition": condition,
                "vid": v.get("id"), "image": _variant_image(v),
                "availability": availability, "ram": ram_from_name(v.get("name", "")),
            }

    results = []
    for (cond_key, storage), d in best.items():
        # Deep-link to this exact variant via its id in the slug's trailing slot.
        variant_url = f"{BASE_URL}{base_slug}/{d['vid']}" if d.get("vid") else url
        results.append({
            "model": model,
            "storage": normalize_storage(storage),
            "ram": d.get("ram"),
            "condition": normalize_condition(d["condition"]),
            "price": d["price"],
            "availability": d["availability"],
            "url": variant_url,
            "img_url": d["image"],
            "rating": rating,
            "review_count": review_count,
        })
    return results


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)

    # Phase 1: get all product URLs (one browser session)
    product_urls = get_product_urls()
    paths = list(product_urls.values())
    print(f"Total products to visit: {len(paths)}\n")

    # Phase 2: fetch + parse each product over plain HTTP, in parallel.
    best = {}  # (variant_key, condition) -> lowest price offer

    def work(path):
        try:
            return parse_product_page(path)
        except Exception as e:
            print(f"  ERROR {path}: {str(e)[:80]}")
            log_error(e, site=SITE, path=path)
            return None

    done = read_ok = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(work, p): p for p in paths}
        for fut in as_completed(futures):
            done += 1
            res = fut.result()
            if res is not None:                      # page read OK (offers may be empty)
                read_ok += 1
            for v in (res or []):
                vkey = make_variant_key(v["model"], v["storage"])
                bkey = (vkey, v["condition"])
                if better_offer(v["availability"], v["price"], best.get(bkey)):
                    best[bkey] = {
                        "model": v["model"], "storage": v["storage"], "ram": v.get("ram"),
                        "variant_key": vkey, "condition": v["condition"],
                        "price": v["price"], "availability": v["availability"],
                        "url": v["url"], "image_url": v["img_url"],
                        "name": f"{v['model']} {v['storage'] or ''}".strip(),
                        "rating": v.get("rating"), "review_count": v.get("review_count"),
                    }
            if done % 50 == 0 or done == len(paths):
                print(f"  {done}/{len(paths)} products processed, {len(best)} offers so far")

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
            condition=condition, rating=o.get("rating"), review_count=o.get("review_count"), url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:40} [{condition:15}] {o['availability']:12} ₹{o['price']:.0f}")

    # Gate the OOS sweep on scraper HEALTH: the fraction of product pages we read
    # successfully (a block makes pages return no RSC -> read_ok collapses -> skip).
    ratio = (read_ok / len(paths)) if paths else 0.0
    run_complete = bool(paths) and ratio >= 0.7
    print(f"Read OK: {read_ok}/{len(paths)} ({ratio*100:.0f}%) — run_complete={run_complete}")
    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=run_complete)

    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
