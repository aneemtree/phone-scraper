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
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock

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
                return []
            time.sleep(2)
        except Exception:
            return []
    if not r or r.status_code != 200:
        return []

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
        return []

    model = clean_model(variants[0].get("name", "") or "")
    if not model or not is_phone(model):
        return []

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
    # truth (qty is unreliable — stays >0 on sold-out variants).
    best = {}  # (cond_lower, storage) -> {price, storage, condition, vid, image}
    for v in variants:
        if (v.get("stock_update") or "").lower() != "in_stock":
            continue
        condition = v.get("condition") or ""
        if re.sub(r"[^a-z]", "", condition.lower()) == "asis":
            continue  # skip the lowest "As-Is" grade — we don't list these
        storage = v.get("storage")
        price = float(v.get("price") or 0)
        if not storage or not price:
            continue
        key = (condition.lower(), storage)
        cur = best.get(key)
        if cur is None or price < cur["price"]:
            best[key] = {
                "price": price, "storage": storage, "condition": condition,
                "vid": v.get("id"), "image": _variant_image(v),
            }

    results = []
    for (cond_key, storage), d in best.items():
        # Deep-link to this exact variant via its id in the slug's trailing slot.
        variant_url = f"{BASE_URL}{base_slug}/{d['vid']}" if d.get("vid") else url
        results.append({
            "model": model,
            "storage": normalize_storage(storage),
            "condition": normalize_condition(d["condition"]),
            "price": d["price"],
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
            return []

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(work, p): p for p in paths}
        for fut in as_completed(futures):
            done += 1
            for v in (fut.result() or []):
                vkey = make_variant_key(v["model"], v["storage"])
                bkey = (vkey, v["condition"])
                if bkey not in best or v["price"] < best[bkey]["price"]:
                    best[bkey] = {
                        "model": v["model"], "storage": v["storage"], "ram": None,
                        "variant_key": vkey, "condition": v["condition"],
                        "price": v["price"], "url": v["url"], "image_url": v["img_url"],
                        "name": f"{v['model']} {v['storage'] or ''}".strip(),
                        "rating": v.get("rating"), "review_count": v.get("review_count"),
                    }
            if done % 50 == 0 or done == len(paths):
                print(f"  {done}/{len(paths)} products processed, {len(best)} offers so far")

    print(f"\nUnique (variant, condition) offers: {len(best)}")

    saved = 0
    for (vkey, condition), o in best.items():
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
            condition=condition, rating=o.get("rating"), review_count=o.get("review_count"), url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:40} [{condition:15}] ₹{o['price']:.0f}")

    # Phones not seen in this run -> out of stock (guarded against partial runs).
    mark_unseen_out_of_stock(SITE, run_started_at)

    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    scrape()
