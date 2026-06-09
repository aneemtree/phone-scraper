"""
Cashify scraper.

Flow:
1. Playwright loads the listing page once, intercepts the x-authorization token.
2. Use the token + requests to paginate the API for all product slugs.
3. For each product, fetch the page over plain HTTP (no browser) and read the
   variant matrix from the embedded Next.js RSC payload. The payload carries one
   object per (grade, storage, color) with sellingPrice, availableInventory and a
   variantId. We keep the lowest in-stock price per (condition, storage) and
   deep-link to that variant.
4. Saves to Supabase.

Why RSC instead of clicking the rendered page: the DOM approach merged condition
labels, mis-read prices, and could not reliably tell in-stock from sold-out. The
RSC payload is the authoritative source:
  - availableInventory > 0  => in stock (sold-out variants are excluded)
  - sellingPrice            => the price shown to the user (non-membership)
  - variantId               => the per-variant URL is {product_slug}/{variantId}

Run with: python3 cashify.py
"""
import re
import json
import time
import requests
from playwright.sync_api import sync_playwright
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone, parse_size_string
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer, months_to_days
from obs import init_sentry, log_error

SITE = "cashify"
BASE_URL = "https://www.cashify.in"
API_URL = "https://www.cashify.in/api/omni01/v1/collection/product/detail"
API_PARAMS = "?ss=%2Fbuy-refurbished-mobile-phones%2Fall-phones"
LISTING_URL = f"{BASE_URL}/buy-refurbished-mobile-phones/all-phones"
DELAY = 0.5
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def capture_token(pw):
    """Load the listing page in a headless browser, intercept the API token."""
    print("Capturing auth token from listing page...")
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
    captured = {}

    def on_request(req):
        if "collection/product/detail" in req.url and req.method == "POST":
            auth = req.headers.get("x-authorization", "")
            if auth and "token" not in captured:
                captured["token"] = auth
                captured["device_id"] = req.headers.get("x-app-device-id", "")

    page.on("request", on_request)
    page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(3000)
    browser.close()

    if not captured.get("token"):
        raise RuntimeError("Failed to capture auth token from Cashify listing page")
    print(f"Token captured: {captured['token'][:60]}...")
    return captured["token"], captured.get("device_id", "")


def fetch_all_products(token, device_id):
    """Paginate the Cashify API to get all product slugs + metadata."""
    headers = {
        "User-Agent": UA,
        "x-authorization": token,
        "x-app-device-id": device_id,
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": LISTING_URL,
    }
    products = []
    page_size = 20
    page_num = 1
    while True:
        r = requests.post(
            API_URL + API_PARAMS,
            json={"ps": page_size, "os": page_num},
            headers=headers,
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  API error {r.status_code} at page {page_num}")
            break
        data = r.json()
        batch = data.get("results", [])
        if not batch:
            break
        products.extend(batch)
        print(f"  fetched {len(products)} products so far (page {page_num})...")
        if len(batch) < page_size:
            break
        page_num += 1
        time.sleep(0.5)
    return products


def decode_payload(html):
    """Reassemble the Next.js RSC payload from the self.__next_f.push() chunks.
    Each chunk is a JSON-encoded string; json.loads un-escapes it, and the
    concatenation is the clean (unescaped) payload we search for variants."""
    chunks = re.findall(r'self\.__next_f\.push\(\[\d+,(".*?")\]\)', html, re.S)
    out = []
    for c in chunks:
        if c.startswith('"'):
            try:
                out.append(json.loads(c))
            except Exception:
                continue
    return "".join(out)


def extract_variant_objects(payload, key='"availableInventory"'):
    """Return the raw JSON text of each variant object. Every variant object
    starts with the availableInventory key, so we anchor there and brace-match
    forward with string-awareness (a price label like "{amount}" lives inside a
    string and must NOT be treated as a real brace)."""
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


def parse_ram_storage(value):
    """Cashify stores the size as 'RAM / Storage' — e.g. '4 GB / 64 GB' or, for
    some brands (Samsung), '12 GB RAM / 512 GB' with the literal word RAM. Use
    the shared parse_size_string(), which strips the RAM keyword and applies the
    RAM-vs-storage sanity rules — the old naive split produced ram='12GBRAM'."""
    return parse_size_string(value)


def fetch_product_variants(slug):
    """GET the product page over plain HTTP and return (url, [variant dicts]),
    de-duplicated by variantId."""
    url = BASE_URL + slug
    r = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(10, 30))
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                return url, []
            time.sleep(2)
        except Exception:
            return url, []
    if not r or r.status_code != 200:
        return url, []

    payload = decode_payload(r.text)
    seen = {}
    for obj in extract_variant_objects(payload):
        try:
            v = json.loads(obj)
        except Exception:
            continue
        vid = v.get("variantId")
        if isinstance(vid, int) and vid not in seen:
            seen[vid] = v
    return url, list(seen.values())


def scrape_product(slug, img_url):
    """Return (product_url, rows). One row per (condition, storage) at the LOWEST
    in-stock price across colors, deep-linked to that variant. Out-of-stock
    variants (availableInventory == 0) are excluded."""
    url, variants = fetch_product_variants(slug)
    if not variants:
        return url, []

    model = clean_model(variants[0].get("productName", "") or "")
    if not model or not is_phone(model):
        return url, []

    best = {}  # (condition, storage, ram) -> {price, vid, image, availability}
    for v in variants:
        avail = (v.get("availableInventory") or 0) > 0  # authoritative signal
        if not avail and not INCLUDE_OOS:
            continue
        grade = v.get("grade")
        if not grade:
            continue
        ram, storage = parse_ram_storage(v.get("storage"))
        if not storage:
            continue
        price = float(v.get("sellingPrice") or 0)
        if not price:
            continue
        key = (normalize_condition(grade), storage, ram)
        availability = "in_stock" if avail else "out_of_stock"
        if better_offer(availability, price, best.get(key)):
            best[key] = {
                "price": price,
                "vid": v.get("variantId"),
                "image": v.get("defaultProductImg") or None,
                "availability": availability,
            }

    rows = []
    for (condition, storage, ram), b in best.items():
        rows.append({
            "model": model, "condition": condition, "storage": storage, "ram": ram,
            "price": b["price"], "availability": b["availability"],
            "url": f"{url}/{b['vid']}" if b.get("vid") else url,
            "image_url": img_url or b.get("image"),
        })
    return url, rows


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)

    with sync_playwright() as pw:
        token, device_id = capture_token(pw)

    products = fetch_all_products(token, device_id)
    print(f"\nTotal products from API: {len(products)}")

    best = {}  # (variant_key, condition) -> best offer
    for idx, prod in enumerate(products, 1):
        slug = prod.get("slug", "")
        if not slug:
            continue

        img_url = prod.get("img_url", "")
        rating = prod.get("ar")
        review_count = int(prod.get("tr", 0)) if prod.get("tr") else None
        warranty = prod.get("warranty_duration", [None])
        warranty = warranty[0] if isinstance(warranty, list) else warranty
        warranty_months = int(warranty) if warranty and str(warranty).isdigit() else None

        try:
            _url, rows = scrape_product(slug, img_url)
        except Exception as e:
            print(f"  [{idx}/{len(products)}] ERROR {slug}: {str(e)[:80]}")
            log_error(e, site=SITE, slug=slug)
            time.sleep(DELAY)
            continue

        for r in rows:
            vkey = make_variant_key(r["model"], r["storage"], r["ram"])
            key = (vkey, r["condition"])
            cand = {
                "model": r["model"], "storage": r["storage"], "ram": r["ram"],
                "variant_key": vkey, "condition": r["condition"],
                "price": r["price"], "availability": r["availability"],
                "url": r["url"], "image_url": r["image_url"],
                "rating": rating, "review_count": review_count,
                "warranty_days": months_to_days(warranty_months),
                "name": f"{r['model']} {r['storage'] or ''}".strip(),
            }
            if better_offer(r["availability"], r["price"], best.get(key)):
                best[key] = cand

        print(f"  [{idx}/{len(products)}] {slug}: {len(rows)} offers")
        time.sleep(DELAY)

    # Save to Supabase. Phone (site+name) is in stock if any offer is.
    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    saved = 0
    for (vkey, cond), o in best.items():
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
            condition=o["condition"], rating=o.get("rating"),
            review_count=o.get("review_count"),
            warranty_days=o.get("warranty_days"), url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:30} [{cond:12}] {o['availability']:12} ₹{o['price']:.0f}")

    # Phones not seen in this run -> out of stock (guarded against partial runs).
    mark_unseen_out_of_stock(SITE, run_started_at)

    print(f"\nDone. Saved {saved} (variant, condition) offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
