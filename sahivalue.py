"""
SahiValue scraper (sahivalue.com) — Zoho Commerce store.

Plain HTML listing + window.zs_product JSON on product pages.

Flow:
1. Paginate listing until duplicate first-card detected
2. Collect unique product URLs
3. Per product: fetch page, parse window.zs_product JSON
4. Iterate variants: skip OOS, group by (grade, storage), keep lowest price across colors
5. Save to Supabase

Run with: python3 sahivalue.py
"""
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from normalize import clean_model, normalize_storage, make_variant_key, parse_size_string, normalize_condition, parse_name_from_listing, is_phone
from db import save_phone, save_price, ensure_image, mark_site_oos

SITE = "sahivalue"
BASE_URL = "https://www.sahivalue.com"
DELAY = 0.5

MOBILE_CATEGORY_URL = f"{BASE_URL}/categories/mobile/293890000000018024"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


def parse_storage_option(opt):
    """Parse storage option string from SahiValue variant options.
    Handles: '12/256GB', '8/128GB', '256GB', '256', '128'
    For slash format like '12/256GB': left=RAM, right=storage
    For bare numbers like '256': treat as storage GB
    """
    if not opt:
        return None, None
    opt = opt.strip()

    # Slash format: "12/256GB" or "8/128 GB"
    slash = re.search(r"^(\d+)\s*/\s*(\d+)\s*(GB|TB)?$", opt, re.I)
    if slash:
        ram_num = slash.group(1)
        storage_num = slash.group(2)
        unit = (slash.group(3) or "GB").upper()
        return f"{ram_num}GB", normalize_storage(f"{storage_num}{unit}")

    # Has GB/TB: "256GB", "512 GB", "1TB"
    if re.search(r"\d+\s*(GB|TB)", opt, re.I):
        return parse_size_string(opt)

    # Bare number: "256", "128" — treat as storage GB
    if re.match(r"^\d+$", opt):
        return None, normalize_storage(f"{opt}GB")

    return None, None


def get_category_urls(cat_url):
    """Paginate a single category until duplicate content detected."""
    urls = {}
    seen_first = set()
    page = 1
    while True:
        url = cat_url if page == 1 else f"{cat_url}?page={page}"
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select(".theme-prod-box")
        if not cards:
            break
        first_name = cards[0].select_one(".theme-prod-name a")
        first_text = first_name.get_text(strip=True) if first_name else ""
        if first_text in seen_first:
            break
        seen_first.add(first_text)
        for card in cards:
            # Skip OOS cards — theme-ribbon-stock indicates out of stock
            if card.select_one(".theme-ribbon-stock"):
                continue
            link = card.select_one("a.theme-prod-link-overlay")
            if not link:
                continue
            href = link["href"]
            prod_url = BASE_URL + href if href.startswith("/") else href
            if prod_url not in urls:
                urls[prod_url] = None
        if len(cards) < 50:
            break
        page += 1
        time.sleep(DELAY)
    return urls


def discover_brand_categories():
    """Discover brand subcategory URLs from the Mobile Phone nav dropdown.
    Loads the homepage and finds links inside the Mobile Phone nav item only.
    """
    r = requests.get(BASE_URL, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")

    # Find the Mobile Phone nav item and get its direct subcategory links
    # The nav structure: li > a (Mobile Phone) + ul/div > a (brand links)
    mobile_nav = None
    for a in soup.select("a[href*='/categories/mobile/']"):
        parent = a.find_parent("li") or a.find_parent("div")
        if parent:
            mobile_nav = parent
            break

    if not mobile_nav:
        print("  Could not find Mobile Phone nav, using category page fallback")
        return []

    seen = set()
    brands = []
    for a in mobile_nav.select("a[href*='/categories/']"):
        href = a.get("href", "")
        full_url = BASE_URL + href if href.startswith("/") else href
        if full_url == MOBILE_CATEGORY_URL or full_url in seen:
            continue
        name = a.get_text(strip=True)
        if name:
            seen.add(full_url)
            brands.append((name, full_url))

    print(f"  Discovered {len(brands)} brand categories under Mobile Phone")
    return brands


def get_listing_urls():
    """Dynamically discover brand categories then collect all product URLs."""
    print("Discovering brand categories...")
    brands = discover_brand_categories()
    if not brands:
        print("  No brands found, falling back to mobile category")
        brands = [("Mobile", MOBILE_CATEGORY_URL)]

    all_urls = {}
    for brand_name, cat_url in brands:
        cat_urls = get_category_urls(cat_url)
        new = {u: v for u, v in cat_urls.items() if u not in all_urls}
        all_urls.update(new)
        if new:
            print(f"  {brand_name}: {len(cat_urls)} products ({len(new)} new)")
        time.sleep(DELAY)
    return all_urls


def fetch_product_variants(prod_url):
    """Fetch product page and parse all variants from window.zs_product JSON.
    Returns list of dicts with model, ram, storage, condition, price, img_url.
    """
    r = requests.get(prod_url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return None, []

    # Extract window.zs_product JSON
    m = re.search(r'window\.zs_product\s*=\s*(\{.*?\});\s*(?:window|var )', r.text, re.S)
    if not m:
        return None, []

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None, []

    product_name = data.get("name", "")
    if not is_phone(product_name, prod_url):
        return product_name, []
    # Parse model, ram, storage from product name first
    model, name_ram, name_storage = parse_name_from_listing(product_name)
    if not model:
        model = clean_model(product_name)

    # Get first image from product level
    images = data.get("images", [])
    prod_img = None
    if images:
        img_url = images[0].get("url", "")
        if img_url:
            prod_img = f"https://cdn2.zohoecommerce.com{img_url}/400x400?storefront_domain=www.sahivalue.com"

    variants = data.get("variants", [])
    results = []

    for v in variants:
        if v.get("is_out_of_stock", True):
            continue

        price = v.get("selling_price")
        if not price:
            continue
        price = float(price)

        # Parse options: [condition, storage, color] or subsets
        options = v.get("options", [])
        opt_values = [o.get("value", "").strip() for o in options]

        # Get condition from custom_fields (most reliable)
        condition = None
        for cf in v.get("custom_fields", []):
            if cf.get("label") == "Grade":
                condition = normalize_condition(cf.get("value", ""))
                break

        # Fallback to options[0] for condition
        if not condition and opt_values:
            condition = normalize_condition(opt_values[0])
        if not condition:
            condition = normalize_condition("Refurbished")

        # Storage from options[1] (e.g. "12/256GB", "8/128GB", "256GB")
        storage_opt = opt_values[1] if len(opt_values) > 1 else ""
        ram, storage = parse_storage_option(storage_opt)

        # If no storage in options, use what we parsed from the product name
        if not storage:
            ram, storage = name_ram, name_storage

        # Image: variant-level first, then product-level
        var_images = v.get("images", [])
        img_url = prod_img
        if var_images:
            vi = var_images[0].get("url", "")
            if vi:
                img_url = f"https://cdn2.zohoecommerce.com{vi}/400x400?storefront_domain=www.sahivalue.com"

        # Variant-specific URL — takes user directly to this condition/storage
        variant_id = v.get("variant_id", "")
        variant_url = f"{prod_url}?variant={variant_id}" if variant_id else prod_url

        results.append({
            "model": model, "ram": ram, "storage": storage,
            "condition": condition, "price": price,
            "img_url": img_url, "url": variant_url,
            "name": f"{model} {storage or ''}".strip(),
        })

    return model, results


def _parse_name_storage(raw):
    """Extract storage from end of product name tokens."""
    tokens = raw.strip().split()
    size_tokens = []
    remaining = list(tokens)
    while remaining:
        last = remaining[-1]
        if re.match(r"^\d+\s*(?:GB|TB)$", last, re.I):
            size_tokens.insert(0, last)
            remaining.pop()
        else:
            break
    name_part = " ".join(remaining)
    model = clean_model(name_part)
    ram, storage = None, None
    if len(size_tokens) == 1:
        storage = normalize_storage(size_tokens[0])
    elif len(size_tokens) >= 2:
        ram, storage = parse_size_string("/".join(size_tokens))
    return model, ram, storage


def scrape():
    mark_site_oos("sahivalue")
    print("Collecting product URLs from listing...")
    url_map = get_listing_urls()
    print(f"Unique products: {len(url_map)}\n")

    # best[(variant_key, condition)] = lowest price offer
    best = {}

    for idx, (prod_url, badge) in enumerate(url_map.items(), 1):
        try:
            model, variants = fetch_product_variants(prod_url)
        except Exception as e:
            print(f"  [{idx}/{len(url_map)}] ERROR {prod_url}: {e}")
            time.sleep(DELAY)
            continue

        if not variants:
            print(f"  [{idx}/{len(url_map)}] {prod_url}: 0 variants")
            time.sleep(DELAY)
            continue

        for v in variants:
            vkey = make_variant_key(v["model"], v["storage"], v["ram"])
            bkey = (vkey, v["condition"])
            if bkey not in best or v["price"] < best[bkey]["price"]:
                best[bkey] = {
                    "model": v["model"], "storage": v["storage"], "ram": v["ram"],
                    "variant_key": vkey, "condition": v["condition"],
                    "price": v["price"], "url": v["url"], "image_url": v["img_url"],
                    "name": f"{v['model']} {v['storage'] or ''}".strip(),
                }

        print(f"  [{idx}/{len(url_map)}] {model}: {len(variants)} available variants")
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
            condition=condition, rating=None, review_count=None, url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:40} [{condition:20}] ₹{o['price']:.0f}")

    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    scrape()