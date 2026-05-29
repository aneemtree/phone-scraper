"""
Xtracover scraper (xtracover.com).

Listing page is JS-rendered (lazy loads on scroll).
All data needed is in the listing cards — no product page visits needed.

Each card has:
  meta[itemprop=name]         → "Apple iPhone 11 (64 GB) Black"
  meta[itemprop=price]        → 18599
  meta[itemprop=availability] → https://schema.org/InStock or OutOfStock
  button[data-stock]          → 0 = in stock, >0 = out of stock (confusing but confirmed)
  a[href]                     → /buy-refurbished/mobiles/brand/slug/grade

Strategy:
  - Scroll listing until no new cards load
  - Parse each card: name, price, availability, grade from URL
  - Skip out-of-stock cards (availability = OutOfStock)
  - Parse storage from name e.g. "(64 GB)" or "(128 GB)"
  - Group by (model, storage, grade) → keep lowest price across colors
  - Save to Supabase

Run with: python3 xtracover.py
"""
import re
import time
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from normalize import clean_model, normalize_storage, make_variant_key, parse_size_string, normalize_condition
from db import save_phone, save_price, ensure_image

SITE = "xtracover"
BASE_URL = "https://www.xtracover.com"
LISTING_URL = f"{BASE_URL}/buy-refurbished/mobiles"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def parse_name(raw):
    """Parse model name and storage from listing card name.
    e.g. "Apple iPhone 11 (64 GB) Black" → model="Apple iPhone 11", storage="64GB"
    """
    # Extract storage from parentheses e.g. (64 GB) or (128GB)
    storage_match = re.search(r"\((\d+\s*(?:GB|TB))\)", raw, re.I)
    storage = normalize_storage(storage_match.group(1)) if storage_match else None

    # Remove storage, color words, and noise from name
    name = raw
    if storage_match:
        name = name[:storage_match.start()].strip()
    # Strip trailing color words (anything after the model number)
    # Model ends at last number token — strip anything after
    name = re.sub(r"\s*-\s*Refurbished.*$", "", name, flags=re.I).strip()

    model = clean_model(name)
    return model, storage


def parse_grade(href):
    """Extract grade from product URL.
    e.g. /buy-refurbished/mobiles/apple/apple-iphone-11---64-gb-black2/refurbishedgood
    → "Good"
    """
    grade_map = {
        "refurbishedgood": "Good",
        "refurbishedexcellent": "Excellent",
        "refurbishedlikenew": "Like New",
        "refurbishedfair": "Fair",
        "refurbished": "Refurbished",
    }
    slug = href.rstrip("/").split("/")[-1].lower()
    return grade_map.get(slug, slug.replace("refurbished", "").title() or "Refurbished")


def get_image(card):
    img = card.select_one("img.productimage")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("//"):
            src = "https:" + src
        return src or None
    return None


def scrape_listing():
    """Use Playwright to scroll-load all cards from the listing page."""
    print("Loading listing page (scrolling to load all cards)...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
        page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        prev = 0
        for _ in range(30):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            count = page.evaluate("document.querySelectorAll('.product-card').length")
            if count == prev:
                break
            prev = count
            print(f"  Cards loaded: {count}")

        html = page.content()
        browser.close()

    return html


def scrape():
    html = scrape_listing()
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".product-card")
    print(f"\nTotal cards found: {len(cards)}")

    # best[(variant_key, grade)] = lowest price offer
    best = {}

    for card in cards:
        # Availability check
        avail_meta = card.select_one("meta[itemprop='availability']")
        avail = (avail_meta["content"] if avail_meta else "").lower()
        if "outofstock" in avail:
            continue

        # Also check data-stock — 0 = in stock
        stock_btn = card.select_one("button[data-stock]")
        if stock_btn and stock_btn.get("data-stock", "0") != "0":
            continue

        # Price
        price_meta = card.select_one("meta[itemprop='price']")
        if not price_meta:
            continue
        try:
            price = float(price_meta["content"])
        except (ValueError, KeyError):
            continue

        # Name → model + storage
        name_meta = card.select_one("meta[itemprop='name']")
        if not name_meta:
            continue
        raw_name = name_meta["content"]
        model, storage = parse_name(raw_name)
        if not model:
            continue

        # Product URL → grade
        link = card.select_one("a[href*='/buy-refurbished/mobiles/']")
        if not link:
            continue
        href = link["href"]
        url = BASE_URL + href if href.startswith("/") else href
        grade = normalize_condition(parse_grade(href))

        # Image
        img_url = get_image(card)

        # Variant key
        vkey = make_variant_key(model, storage)
        bkey = (vkey, grade)

        if bkey not in best or price < best[bkey]["price"]:
            best[bkey] = {
                "model": model, "storage": storage, "ram": None,
                "variant_key": vkey, "grade": grade, "price": price,
                "url": url, "image_url": img_url,
                "name": f"{model} {storage or ''}".strip(),
            }

    print(f"Unique (variant, grade) offers: {len(best)}")

    # Save to Supabase
    saved = 0
    for (vkey, grade), o in best.items():
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
            condition=grade, rating=None, review_count=None,
        )
        saved += 1
        print(f"  saved: {o['name']:35} [{grade:15}] ₹{o['price']:.0f}")

    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    scrape()
