"""
Cashify scraper.

Flow:
1. Playwright loads the listing page, intercepts the x-authorization token.
2. Use the token + requests to paginate the API (no browser needed for listing).
3. For each product, Playwright visits the product page, clicks through each
   available condition (Fair/Good/Superb), clicks each available storage,
   reads the price. Keeps lowest price per (variant_key, condition).
4. Saves to Supabase.

Key HTML insight: condition/storage options are <div style="cursor:pointer">,
not <button>. Selected = border-secondary-border, unavailable = opacity-50.

Run with: python3 cashify.py
"""
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from normalize import clean_model, normalize_storage, normalize_ram, make_variant_key, parse_size_string, normalize_condition
from db import save_phone, save_price, ensure_image

SITE = "cashify"
BASE_URL = "https://www.cashify.in"
API_URL = "https://www.cashify.in/api/omni01/v1/collection/product/detail"
API_PARAMS = "?ss=%2Fbuy-refurbished-mobile-phones%2Fall-phones"
LISTING_URL = f"{BASE_URL}/buy-refurbished-mobile-phones/all-phones"
DELAY = 1.5
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


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


def parse_price(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return float(digits) if digits else None


def get_option_divs(page, section_heading):
    """Get options for a section (Condition/Storage).
    Structure: h2 -> parent (heading row) -> parent (section)
               section.children[1].children[0] = the options flex-row
    Each option: wrapper div > inner div (style="cursor:pointer")
    Available = inner div does NOT have opacity-50 AND no .line-through child."""
    js = """(heading) => {
        const h2s = Array.from(document.querySelectorAll('h2'));
        const h = h2s.find(e => e.textContent.trim() === heading);
        if (!h) return [];
        const section = h.parentElement.parentElement;
        if (!section || !section.children[1] || !section.children[1].children[0]) return [];
        const optRow = section.children[1].children[0];
        return Array.from(optRow.children).map(wrapper => {
            const inner = wrapper.firstElementChild;
            const hasOpacity = inner ? inner.classList.contains('opacity-50') : true;
            const hasLineThrough = !!wrapper.querySelector('.line-through');
            return {
                text: wrapper.innerText.trim(),
                available: !hasOpacity && !hasLineThrough,
            };
        });
    }"""
    return page.evaluate(js, section_heading)


def click_option(page, section_heading, option_text):
    """Click the inner div of an option matching option_text under section_heading."""
    js = """([heading, optText]) => {
        const h2s = Array.from(document.querySelectorAll('h2'));
        const h = h2s.find(e => e.textContent.trim() === heading);
        if (!h) return false;
        const section = h.parentElement.parentElement;
        if (!section || !section.children[1] || !section.children[1].children[0]) return false;
        const optRow = section.children[1].children[0];
        for (const wrapper of optRow.children) {
            if (wrapper.innerText.trim().includes(optText)) {
                const inner = wrapper.firstElementChild;
                if (inner) { inner.click(); return true; }
                wrapper.click(); return true;
            }
        }
        return false;
    }"""
    return page.evaluate(js, [section_heading, option_text])


def read_price(page):
    """Read the main product price from the page."""
    js = """() => {
        // Price is in an h3 with class containing 'h3' near a rupee symbol
        const els = Array.from(document.querySelectorAll('h3, h2, [class*="h3"]'));
        for (const el of els) {
            const t = el.innerText.trim();
            if (/^\u20b9[\\d,]+$/.test(t)) return t;
        }
        // Fallback: find any element with a rupee price
        const all = Array.from(document.querySelectorAll('*'));
        for (const el of all) {
            if (el.children.length === 0) {
                const t = el.innerText.trim();
                if (/^\u20b9[\\d,]{4,}$/.test(t)) return t;
            }
        }
        return null;
    }"""
    return page.evaluate(js)



def try_find_available_price(page):
    """Click through ALL available colors for the current condition+storage,
    read the price for each, and return the LOWEST price found (or None)."""
    js_colors = """() => {
        const swatches = Array.from(document.querySelectorAll(
            '.rounded-full[style*="cursor: pointer"]'
        ));
        return swatches.map((el, i) => {
            const inner = el.querySelector('.rounded-full');
            const hasOpacity = el.classList.contains('opacity-50') ||
                (inner && inner.classList.contains('opacity-50'));
            const hasCross = !!el.querySelector('path[d*="24.971"]');
            return { index: i, available: !hasOpacity && !hasCross };
        });
    }"""
    js_click = """(idx) => {
        const swatches = Array.from(document.querySelectorAll(
            '.rounded-full[style*="cursor: pointer"]'
        ));
        if (swatches[idx]) { swatches[idx].click(); return true; }
        return false;
    }"""

    colors = page.evaluate(js_colors)
    available_colors = [c for c in colors if c["available"]]

    if not available_colors:
        # No color swatches — just read current price
        return parse_price(read_price(page))

    prices = []
    for color in available_colors:
        page.evaluate(js_click, color["index"])
        page.wait_for_timeout(400)
        price = parse_price(read_price(page))
        if price:
            prices.append(price)

    return min(prices) if prices else None

def scrape_product(page, slug, product_name, img_url, rating, review_count, warranty_months):
    """Visit a product page and scrape per-condition, per-storage prices."""
    url = BASE_URL + slug
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector('h2', timeout=15000)
    except Exception:
        return url, []
    page.wait_for_timeout(2000)

    results = []
    conditions = get_option_divs(page, "Condition")
    if not conditions:
        # No condition selector — try to read a single price
        price = parse_price(read_price(page))
        if price:
            results.append({
                "condition": "Refurbished", "storage": None, "ram": None,
                "price": price, "url": url,
            })
        return url, results

    available_conditions = [c for c in conditions if c["available"]]
    if not available_conditions:
        # All conditions unavailable on product page — skip entirely
        return url, []

    for cond in available_conditions:
        cond_text = cond["text"].strip()
        click_option(page, "Condition", cond_text)
        page.wait_for_timeout(800)

        # Get available storage options for this condition
        storages = get_option_divs(page, "Storage")
        available_storages = [s for s in storages if s["available"]] if storages else []

        if not available_storages:
            # No storage selector — read single price
            price = parse_price(read_price(page))
            if price:
                results.append({
                    "condition": cond_text, "storage": None, "ram": None,
                    "price": price, "url": url,
                })
            continue

        for stor in available_storages:
            stor_text = stor["text"].strip()
            click_option(page, "Storage", stor_text)
            page.wait_for_timeout(600)

            # Try colors if default color shows no price (might be OOS in that color)
            price = try_find_available_price(page)
            if price is None:
                continue

            # Parse RAM and storage using shared normalize helper
            ram, storage = parse_size_string(stor_text)

            # Skip if no storage parsed
            if not storage:
                continue

            results.append({
                "condition": normalize_condition(cond_text), "storage": storage, "ram": ram,
                "price": price, "url": url,
            })

    return url, results


def scrape():
    with sync_playwright() as pw:
        # Step 1: capture token
        token, device_id = capture_token(pw)

        # Step 2: fetch all products from API
        products = fetch_all_products(token, device_id)
        print(f"\nTotal products from API: {len(products)}")

        # Step 3: visit each product page for per-condition prices
        best = {}  # (variant_key, condition) -> best offer

        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})

        for idx, prod in enumerate(products, 1):
            slug = prod.get("slug", "")
            if not slug:
                continue

            product_name = prod.get("product_name", "").replace(" - Refurbished", "").strip()
            img_url = prod.get("img_url", "")
            rating = prod.get("ar")
            review_count = int(prod.get("tr", 0)) if prod.get("tr") else None
            warranty = prod.get("warranty_duration", [None])[0]
            warranty_months = int(warranty) if warranty and str(warranty).isdigit() else None

            try:
                url, rows = scrape_product(
                    page, slug, product_name, img_url,
                    rating, review_count, warranty_months
                )
            except Exception as e:
                print(f"  [{idx}/{len(products)}] ERROR {slug}: {str(e)[:80]}")
                time.sleep(DELAY)
                continue

            for r in rows:
                model = clean_model(product_name)
                storage = normalize_storage(r["storage"]) if r["storage"] else None
                ram = r["ram"]
                vkey = make_variant_key(model, storage, ram)
                key = (vkey, r["condition"])

                cand = {
                    "model": model, "storage": storage, "ram": ram,
                    "variant_key": vkey, "condition": r["condition"],
                    "price": r["price"], "url": r["url"],
                    "image_url": img_url, "rating": rating,
                    "review_count": review_count, "warranty_months": warranty_months,
                    "name": f"{model} {storage or ''}".strip(),
                }
                if key not in best or r["price"] < best[key]["price"]:
                    best[key] = cand

            print(f"  [{idx}/{len(products)}] {slug}: {len(rows)} price points")
            time.sleep(DELAY)

        browser.close()

    # Step 4: save to Supabase
    saved = 0
    for (vkey, cond), o in best.items():
        # Self-host image on first sighting
        hosted = None
        if o["image_url"]:
            ext = ".jpg"
            dest = f"{SITE}/{o['variant_key']}{ext}".replace("|", "_")
            hosted = ensure_image(o["image_url"], dest)
        final_image = hosted or o["image_url"]

        pid = save_phone(
            SITE, o["name"], o["url"], final_image,
            o["model"], o["storage"], o["ram"], o["variant_key"]
        )
        save_price(
            pid, o["price"], availability="in_stock",
            condition=o["condition"], rating=o.get("rating"),
            review_count=o.get("review_count"),
        )
        saved += 1
        print(f"  saved: {o['name']:30} [{cond:12}] ₹{o['price']:.0f}")

    print(f"\nDone. Saved {saved} (variant, condition) offers from {SITE}.")


if __name__ == "__main__":
    scrape()