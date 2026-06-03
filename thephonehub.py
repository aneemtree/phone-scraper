"""
ThePhoneHub scraper (thephonehub.in) — refurbished phones.

WooCommerce store on WordPress. Requests-only (no browser):
  - Listing + product metadata come from the public WooCommerce Store API
    (/wp-json/wc/store/v1/products?category=160 — "Refurbished Smartphones").
  - Per-variant prices/stock come from the variation matrix embedded in each
    product page as `data-product_variations="<html-escaped JSON>"`.

Availability (source of truth): the rendered buy state — `is_purchasable` plus
the `stock_availability` badge at product level, and `is_in_stock` /
`is_purchasable` / `variation_is_active` per variation. The top-level
`is_in_stock` flag is phantom here (always true even for sold-out items), so it
is NOT trusted (per the standard availability rule).

No condition grades on this store — products vary only by Storage and Colour. So
we save one offer per storage variant at the LOWEST price across in-stock colors,
condition "Refurbished".

Prices: variation `display_price` is already in rupees; the Store API
`prices.price` is in minor units (÷100). Out-of-stock products report price 0 in
the Store API, so OOS prices must come from the variation JSON.

Deep-link: permalink + ?attribute_pa_storage=<slug>&attribute_pa_color=<slug>.

Run with: python3 thephonehub.py
"""
import re
import html
import time
import json
import requests
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone, parse_size_string
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer
from obs import init_sentry, log_error

SITE = "thephonehub"
BASE_URL = "https://thephonehub.in"
CATEGORY_ID = 160  # "Refurbished Smartphones"
DELAY = 0.3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}
CONDITION = normalize_condition("Refurbished")


def title_to_model(name):
    """Strip the marketing tail and clean a product title into a model name.
    The model is always the text before the first '(' (storage is parenthesised)
    or, failing that, before the first dash/pipe separator."""
    name = html.unescape(name or "")
    if "(" in name:
        head = name.split("(", 1)[0]
    else:
        head = re.split(r"[–—|]", name, 1)[0]
    return clean_model(head)


def storage_slug_to_size(slug):
    """'12gb-128gb' -> (ram, storage) via the shared size parser.
    The pa_storage slug joins ram and storage with '-'; some are storage-only."""
    if not slug:
        return None, None
    return parse_size_string(slug.replace("-", "|"))


def ram_storage_from_title(name):
    """Best-effort (ram, storage) from a title for simple products with no
    pa_storage attribute (e.g. 'Vivo T1 Pro … 8GB RAM, 128GB Storage')."""
    toks = re.findall(r"\d+\s*(?:GB|TB)", html.unescape(name or ""), re.I)
    if not toks:
        return None, None
    if len(toks) == 1:
        return None, normalize_storage(toks[0])
    return parse_size_string("|".join(toks[:2]))


def fetch_listing():
    """All products in the refurbished-smartphones category (paginated)."""
    products, page = [], 1
    while True:
        url = (f"{BASE_URL}/wp-json/wc/store/v1/products"
               f"?category={CATEGORY_ID}&per_page=100&page={page}")
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(DELAY)
    return products


def fetch_variations(slug):
    """Parse the embedded variation matrix from a product page.
    Returns a list of dicts: {storage_slug, color_slug, ram, storage, price,
    in_stock}. Empty if the page has no variations form (simple product)."""
    url = f"{BASE_URL}/product/{slug}/"
    try:
        page = requests.get(url, headers=HEADERS, timeout=30).text
    except Exception:
        return []
    m = re.search(r'data-product_variations="(.*?)"', page, re.S)
    if not m:
        return []
    try:
        raw = json.loads(html.unescape(m.group(1)))
    except (ValueError, TypeError):
        return []
    out = []
    for v in raw:
        attrs = v.get("attributes") or {}
        storage_slug = attrs.get("attribute_pa_storage")
        color_slug = attrs.get("attribute_pa_color")
        ram, storage = storage_slug_to_size(storage_slug)
        price = v.get("display_price")
        if not storage or not price:
            continue
        in_stock = bool(v.get("is_in_stock") and v.get("is_purchasable")
                        and v.get("variation_is_active"))
        out.append({
            "storage_slug": storage_slug, "color_slug": color_slug,
            "ram": ram, "storage": storage, "price": float(price),
            "in_stock": in_stock,
        })
    return out


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)

    products = fetch_listing()
    print(f"Fetched {len(products)} products in category {CATEGORY_ID}")

    best = {}  # (variant_key, ram, condition) -> lowest-price offer

    for prod in products:
        raw_name = prod.get("name", "")
        model = title_to_model(raw_name)
        if not model or not is_phone(model, prod.get("slug", "")):
            continue

        badge = (prod.get("stock_availability") or {}).get("class", "")
        purchasable = bool(prod.get("is_purchasable"))
        product_in_stock = purchasable and badge == "in-stock"
        if not product_in_stock and not INCLUDE_OOS:
            continue

        slug = prod.get("slug", "")
        permalink = prod.get("permalink") or f"{BASE_URL}/product/{slug}/"
        images = prod.get("images") or []
        prod_img = images[0].get("src") if images else None

        attrs = prod.get("attributes") or []
        storage_attr = next((a for a in attrs if a.get("taxonomy") == "pa_storage"), None)
        storage_terms = (storage_attr or {}).get("terms") or []
        is_variable = prod.get("type") == "variable"

        # Build the list of (storage, ram, price, in_stock, storage_slug,
        # color_slug) tuples to consider for this product.
        offers = []
        need_page = is_variable and (INCLUDE_OOS or len(storage_terms) > 1)
        if need_page:
            for v in fetch_variations(slug):
                if not v["in_stock"] and not INCLUDE_OOS:
                    continue
                offers.append(v)
            time.sleep(DELAY)
        else:
            # Single price path: trust the Store API min price (which WooCommerce
            # computes across purchasable variations) + the single storage term.
            price_minor = (prod.get("prices") or {}).get("price")
            try:
                price = float(price_minor) / 100.0
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                continue  # OOS/simple with no usable Store API price
            if storage_terms:
                term = storage_terms[0]
                ram, storage = storage_slug_to_size(term.get("slug"))
                storage_slug = term.get("slug")
            else:
                ram, storage = ram_storage_from_title(raw_name)
                storage_slug = None
            if storage:
                offers.append({
                    "storage": storage, "ram": ram, "price": price,
                    "in_stock": product_in_stock,
                    "storage_slug": storage_slug, "color_slug": None,
                })

        for o in offers:
            storage, ram, price = o["storage"], o["ram"], o["price"]
            vkey = make_variant_key(model, storage, ram)
            availability = "in_stock" if o["in_stock"] else "out_of_stock"
            bkey = (vkey, ram, CONDITION)
            if not better_offer(availability, price, best.get(bkey)):
                continue
            url = permalink
            params = []
            if o.get("storage_slug"):
                params.append(f"attribute_pa_storage={o['storage_slug']}")
            if o.get("color_slug"):
                params.append(f"attribute_pa_color={o['color_slug']}")
            if params:
                url = f"{permalink}?{'&'.join(params)}"
            best[bkey] = {
                "model": model, "storage": storage, "ram": ram,
                "variant_key": vkey, "price": price, "availability": availability,
                "url": url, "image_url": prod_img,
                "name": (f"{model} {ram}/{storage}" if ram else f"{model} {storage}").strip(),
            }

    print(f"\nUnique (variant, condition) offers: {len(best)}")

    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    saved = 0
    for o in best.values():
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
            condition=CONDITION, url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:42} ₹{o['price']:.0f}  [{o['availability']}]")

    mark_unseen_out_of_stock(SITE, run_started_at)
    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
