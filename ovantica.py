"""
Ovantica scraper (ovantica.com) — refurbished smartphones.

Two-phase approach:
  Phase 1: Playwright listing page — click Load More, intercept _rsc= URLs to get all product URLs
  Phase 2: Plain requests per product page — parse RSC payload for full variant data
           (storage, condition, color, price, qty)

Variant structure in RSC payload:
  storage -> colors[] -> conditions[] -> {condition, price, qty, storage, color, name, image}

Only saves variants with qty > 0 (in stock).
Groups by (model, storage, condition), keeps lowest price across colors.

Run with: python3 ovantica.py
"""
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright.sync_api import sync_playwright
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone
from db import save_phone, save_price, ensure_image, mark_site_oos

SITE = "ovantica"
BASE_URL = "https://ovantica.com"
LISTING_URL = f"{BASE_URL}/buy-refurbished-smartphones"
CDN = "https://cdn.ovantica.com/cdn-cgi/image/width=400,quality=80,format=auto/images/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}
DELAY = 0.3


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


def check_conditions_playwright(url):
    """Use Playwright to click each condition button and check availability.
    Returns a list of {"condition": <text>} dicts for conditions that are in
    stock (Add to Cart present). Price/storage come from the RSC payload.
    """
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

            # Get all condition buttons
            cond_buttons = page.locator("[data-testid^='condition-']").all()

            for btn in cond_buttons:
                try:
                    condition_text = btn.inner_text().strip()
                    # Skip the lowest "As-Is" grade — we don't list these.
                    if re.sub(r"[^a-z]", "", condition_text.lower()) == "asis":
                        continue
                    btn.click()
                    page.wait_for_timeout(800)

                    # In stock if the Add to Cart button is present for this
                    # condition. Price is NOT read here — it comes from the RSC
                    # payload, which is authoritative; the rendered price element
                    # was unreliable (EMI/strike values).
                    has_cart = page.locator("[data-testid='button-add-to-cart']").count() > 0
                    if not has_cart:
                        continue

                    results.append({"condition": condition_text})
                except Exception:
                    continue
        except Exception as e:
            pass
        finally:
            browser.close()
    return results


def parse_product_page(path):
    """Fetch product page. Use plain requests for fast OOS check.
    If product has any in-stock variant, use Playwright to check per-condition availability.
    """
    url = BASE_URL + path
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
    if r.status_code != 200:
        return []

    # If no Add to Cart at all — entire product OOS, skip
    if "Out of Stock" in r.text and "button-add-to-cart" not in r.text:
        return []

    # Parse base product data from RSC payload
    chunks = re.findall(r'self\.__next_f\.push\(\[\d+,(".*?")\]\)', r.text, re.S)
    payload = "".join(json.loads(c) for c in chunks if c.startswith('"'))

    variant_pattern = (
        r'\{"condition":"([^"]+)","id":(\d+),"sku":"[^"]*","name":"([^"]+)",'
        r'"color":"([^"]+)","rom":"[^"]*","price":(\d+),"strikeAmt":"[^"]*",'
        r'"qty":(\d+),[^}]*"storage":"([^"]+)",[^}]*"image":"((?:[^"\\]|\\.)*)"'
    )
    matches = re.findall(variant_pattern, payload)
    if not matches:
        return []

    model = clean_model(matches[0][2])
    if not model or not is_phone(model):
        return []

    # The trailing number in an Ovantica URL is a per-VARIANT id (it changes as
    # you pick storage/condition/color), not a separate product page. Each variant
    # object carries its own "id", so we deep-link to that exact variant by
    # swapping the slug's trailing id for the chosen variant's id.
    base_slug = re.sub(r"/\d+/?$", "", path)

    # Get product image (group 8 = image of the first variant)
    prod_img = None
    try:
        clean = matches[0][7].replace('\\"', '"').replace("\\'", "'")
        imgs = json.loads(clean)
        if imgs:
            prod_img = CDN + imgs[0]
    except Exception:
        pass

    # Extract rating
    rating, review_count = None, None
    rm = re.search(r'"ratingValue":"([^"]+)".*?"reviewCount":"([^"]+)"', payload)
    if rm:
        try:
            rating = float(rm.group(1))
            review_count = int(rm.group(2))
        except (ValueError, TypeError):
            pass

    # Build per-(condition, storage) variants straight from the payload, which
    # carries the AUTHORITATIVE price. (The rendered price element is unreliable
    # — it was picking up EMI/strike values, making every price wrong.) For each
    # (condition, storage) keep the LOWEST price across colors, per the price rule,
    # and only consider variants the payload marks in stock (qty > 0).
    variants_by_key = {}  # (cond_key, storage) -> {price, storage, condition, image_raw, vid}
    for condition, vid, name, color, price, qty, storage, image_raw in matches:
        if int(qty) <= 0:
            continue
        # Skip the lowest "As-Is" grade — we don't list these.
        if re.sub(r"[^a-z]", "", condition.lower()) == "asis":
            continue
        price = float(price)
        key = (condition.lower(), storage)
        cur = variants_by_key.get(key)
        if cur is None or price < cur["price"]:
            variants_by_key[key] = {
                "price": price, "storage": storage,
                "condition": condition, "image_raw": image_raw, "vid": vid,
            }

    # Use Playwright to confirm which CONDITIONS are actually purchasable on the
    # rendered page (availability source of truth). If it returns nothing (page
    # structure changed), fall back to the payload's qty>0 signal alone.
    in_stock_conditions = check_conditions_playwright(url)
    available_conds = {c["condition"].lower() for c in in_stock_conditions}

    results = []
    for (cond_key, storage), data in variants_by_key.items():
        if available_conds and cond_key not in available_conds:
            continue

        img_url = prod_img
        try:
            clean = data["image_raw"].replace('\\"', '"').replace("\\'", "'")
            imgs = json.loads(clean)
            if imgs:
                img_url = CDN + imgs[0]
        except Exception:
            pass

        norm_storage = normalize_storage(storage)
        # Deep-link to this exact variant via its id in the slug's trailing slot.
        variant_url = f"{BASE_URL}{base_slug}/{data['vid']}" if data.get("vid") else url

        results.append({
            "model": model,
            "storage": norm_storage,
            "condition": normalize_condition(data["condition"]),
            "color": "",
            "price": data["price"],
            "url": variant_url,
            "img_url": img_url,
            "rating": rating,
            "review_count": review_count,
        })

    return results


def scrape():
    mark_site_oos(SITE)

    # Phase 1: get all product URLs
    product_urls = get_product_urls()
    print(f"Total products to visit: {len(product_urls)}\n")

    # Phase 2: visit each product page, parse variants
    best = {}  # (variant_key, condition) -> lowest price offer

    for idx, (slug, path) in enumerate(product_urls.items(), 1):
        try:
            variants = parse_product_page(path)
        except Exception as e:
            print(f"  [{idx}/{len(product_urls)}] ERROR {path}: {e}")
            time.sleep(DELAY)
            continue

        in_stock = [v for v in variants]
        if not in_stock:
            time.sleep(DELAY)
            continue

        for v in in_stock:
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

        model_name = in_stock[0]["model"] if in_stock else path
        print(f"  [{idx}/{len(product_urls)}] {model_name}: {len(in_stock)} in-stock variants")
        time.sleep(DELAY)

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

    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    scrape()