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
    product_urls = {}  # id -> full path

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})

        def on_response(resp):
            m = re.search(r"(/buy-refurbished[^?]+/(\d+))\?_rsc=", resp.url)
            if m:
                path, pid = m.group(1), m.group(2)
                if pid not in product_urls:
                    product_urls[pid] = path

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
    Returns list of (condition, storage, price) tuples that are in stock.
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

                    # Check if add to cart button is present
                    has_cart = page.locator("[data-testid='button-add-to-cart']").count() > 0
                    if not has_cart:
                        continue

                    # Get current price
                    price_el = page.locator("[data-testid^='price-']").first
                    price_text = price_el.inner_text().strip() if price_el.count() > 0 else ""
                    price_digits = "".join(c for c in price_text if c.isdigit())
                    if not price_digits:
                        continue
                    price = float(price_digits)

                    results.append({
                        "condition": condition_text,
                        "price": price,
                    })
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
        r'\{"condition":"([^"]+)","id":\d+,"sku":"[^"]*","name":"([^"]+)",'
        r'"color":"([^"]+)","rom":"[^"]*","price":(\d+),"strikeAmt":"[^"]*",'
        r'"qty":\d+,[^}]*"storage":"([^"]+)",[^}]*"image":"((?:[^"\\]|\\.)*)"'
    )
    matches = re.findall(variant_pattern, payload)
    if not matches:
        return []

    model = clean_model(matches[0][1])
    if not model or not is_phone(model):
        return []

    # Get product image
    prod_img = None
    try:
        clean = matches[0][5].replace('\\"', '"').replace("\\'", "'")
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

    # Build storage/price lookup from payload variants
    storage_by_condition = {}
    for condition, name, color, price, storage, image_raw in matches:
        cond_key = condition.lower()
        if cond_key not in storage_by_condition:
            storage_by_condition[cond_key] = {
                "storage": storage, "price": float(price), "image_raw": image_raw
            }

    # Use Playwright to check which conditions are actually in stock
    in_stock_conditions = check_conditions_playwright(url)

    results = []
    for item in in_stock_conditions:
        cond_text = item["condition"]
        price = item["price"]
        cond_key = cond_text.lower()

        # Get storage for this condition from payload
        payload_data = storage_by_condition.get(cond_key, {})
        storage = payload_data.get("storage", "")
        image_raw = payload_data.get("image_raw", "")

        img_url = prod_img
        try:
            clean = image_raw.replace('\\"', '"').replace("\\'", "'")
            imgs = json.loads(clean)
            if imgs:
                img_url = CDN + imgs[0]
        except Exception:
            pass

        results.append({
            "model": model,
            "storage": normalize_storage(storage),
            "condition": normalize_condition(cond_text),
            "color": "",
            "price": price,
            "url": url,
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

    for idx, (pid, path) in enumerate(product_urls.items(), 1):
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