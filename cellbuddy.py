"""
CellBuddy scraper (cellbuddy.in) — refurbished iPhones. WordPress/WooCommerce
(installed under the /buddy/ subpath). Requests-only:
  - Listing + metadata from the public Store API
    (/buddy/wp-json/wc/store/v1/products?category=94 — "iPhone").
  - Per-variant price/stock from the product page's embedded
    `data-product_variations` JSON; if absent (above the ajax threshold), the
    matrix is enumerated via ?wc-ajax=get_variation; single-storage products use
    the Store API min price directly.

Condition: there is NO grade variant axis (variants are only Storage × Color).
CellBuddy instead lists each condition as a SEPARATE product, distinguished by
category membership:
  - "No Face ID"  -> condition "No Face ID"
  - "No Touch ID" -> condition "No Touch ID"
  - plain (no extra category) OR "Refurbished" -> "Unknown Condition"
The condition suffix ("- No Face ID", "- Refurbished") is stripped from the model
name; the same model therefore shows several condition rows under one card.

Storage slugs are bare ("128") so storage is read from the attribute TERM NAME
("128GB") via a slug->name map, not the slug.

Prices: variation display_price is rupees; Store API prices.price is minor units
(÷100). Availability: is_purchasable + the stock badge + per-variation is_in_stock
(the top-level is_in_stock is phantom). Deep-link: permalink +
?attribute_pa_storage=&attribute_pa_color=.

Run with: python3 cellbuddy.py
"""
import re
import html
import time
import json
import itertools
import requests
from normalize import clean_model, normalize_storage, make_variant_key, is_phone
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock, INCLUDE_OOS, better_offer
from obs import init_sentry, log_error

SITE = "cellbuddy"
BASE_URL = "https://cellbuddy.in/buddy"
CATEGORY_ID = 94  # "iPhone"
DELAY = 0.3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}
def condition_for(prod):
    """Condition from category membership (no grade axis on this store).
    CellBuddy prices its plain and "Refurbished" listings differently for the
    same model+storage, so they are kept as SEPARATE conditions (both rows show):
      - plain (no condition category) -> "Unknown Condition" (truly unlabelled)
      - "Refurbished" category        -> "Refurbished" (kept literal here; the
        global normalize_condition Refurbished->Unknown remap is for the OTHER
        stores' vague default only)
      - "No Face ID" / "No Touch ID"  -> kept as-is
    """
    cats = {(c.get("name") or "") for c in (prod.get("categories") or [])}
    if "No Face ID" in cats:
        return "No Face ID"
    if "No Touch ID" in cats:
        return "No Touch ID"
    if "Refurbished" in cats:
        return "Refurbished"
    return "Unknown Condition"  # plain / unlabelled


def model_from_name(name):
    """Clean the model: drop the condition suffix and keep generation tokens."""
    name = html.unescape(name or "")
    # Keep "(3rd Generation)" as "3rd Generation" so clean_model's paren-strip
    # doesn't drop it (SE 2nd vs 3rd gen must stay distinct).
    name = re.sub(r"\((\d+(?:st|nd|rd|th)\s+Generation)\)", r" \1", name, flags=re.I)
    # Strip the condition suffix (the category already carries it).
    name = re.sub(r"\bno\s+face\s+id\b", " ", name, flags=re.I)
    name = re.sub(r"\bno\s+touch\s+id\b", " ", name, flags=re.I)
    return clean_model(name)


def storage_map(prod):
    """slug -> normalized storage, from the pa_storage attribute terms
    (variation slugs are bare like '128'; the term NAME is '128GB')."""
    out = {}
    for a in (prod.get("attributes") or []):
        if a.get("taxonomy") == "pa_storage":
            for t in (a.get("terms") or []):
                out[t.get("slug")] = normalize_storage(t.get("name"))
    return out


def fetch_listing():
    products, page = [], 1
    while True:
        url = (f"{BASE_URL}/wp-json/wc/store/v1/products"
               f"?category={CATEGORY_ID}&per_page=100&page={page}")
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            # 403 = Cloudflare WAF block (needs SCRAPER_PROXY); log so it's not a
            # silent 0 in triage.
            print(f"  cellbuddy listing HTTP {r.status_code} (blocked? set SCRAPER_PROXY)")
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


def parse_embedded(page, smap):
    """Embedded variation list -> [{storage, color_slug, storage_slug, price,
    in_stock}], or None if no usable form."""
    m = re.search(r'data-product_variations="(.*?)"', page, re.S)
    if not m:
        return None
    try:
        raw = json.loads(html.unescape(m.group(1)))
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, list):
        return None
    out = []
    for v in raw:
        attrs = v.get("attributes") or {}
        sslug = attrs.get("attribute_pa_storage")
        storage = smap.get(sslug) or normalize_storage(sslug)
        price = v.get("display_price")
        if not storage or not price:
            continue
        out.append({
            "storage": storage, "storage_slug": sslug,
            "color_slug": attrs.get("attribute_pa_color"),
            "price": float(price),
            "in_stock": bool(v.get("is_in_stock") and v.get("is_purchasable")
                             and v.get("variation_is_active")),
        })
    return out


def ajax_matrix(product_id, storage_terms, color_terms, smap):
    out = []
    storages = [t.get("slug") for t in storage_terms] or [None]
    colors = [t.get("slug") for t in color_terms] or [None]
    for sslug, cslug in itertools.product(storages, colors):
        data = {"product_id": str(product_id)}
        if sslug:
            data["attribute_pa_storage"] = sslug
        if cslug:
            data["attribute_pa_color"] = cslug
        try:
            d = requests.post(f"{BASE_URL}/?wc-ajax=get_variation",
                              headers=HEADERS, data=data, timeout=30).json()
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("variation_id") or d.get("display_price") is None:
            continue
        storage = smap.get(sslug) or normalize_storage(sslug)
        if not storage:
            continue
        out.append({
            "storage": storage, "storage_slug": sslug, "color_slug": cslug,
            "price": float(d["display_price"]),
            "in_stock": bool(d.get("is_in_stock") and d.get("is_purchasable")),
        })
        time.sleep(DELAY)
    return out


def _api_range(prod):
    """(min, max) advertised price in rupees from the Store API, or (None, None)."""
    pr = prod.get("prices") or {}
    def f(x):
        try:
            return float(x) / 100.0
        except (TypeError, ValueError):
            return None
    rng = pr.get("price_range")
    if rng:
        return f(rng.get("min_amount")), f(rng.get("max_amount"))
    p = f(pr.get("price"))
    return p, p


def _consistent(variations, prod):
    """True if the embedded variation prices agree with the advertised Store API
    price_range. Some CellBuddy products embed the WRONG variation matrix (e.g.
    plain "iPhone 13" carries iPhone 13 *Pro* variations/prices); those are
    rejected so we fall back to the advertised price instead of a phantom one."""
    lo, hi = _api_range(prod)
    if lo is None or hi is None:
        return True  # nothing to validate against
    prices = [v["price"] for v in variations]
    if not prices:
        return True
    # Reject if the whole variation set sits outside the advertised band (±10%).
    return not (min(prices) > hi * 1.1 or max(prices) < lo * 0.9)


def _store_api_single(prod, smap):
    """Advertised Store API min price as one offer at the cheapest storage term."""
    try:
        price = float((prod.get("prices") or {}).get("price")) / 100.0
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return []
    terms = [a for a in (prod.get("attributes") or []) if a.get("taxonomy") == "pa_storage"]
    terms = terms[0].get("terms") if terms else []
    # cheapest storage = smallest capacity
    def gb(t):
        s = smap.get(t.get("slug")) or normalize_storage(t.get("name")) or ""
        m = re.search(r"(\d+)(TB|GB)", s)
        return (int(m.group(1)) * (1024 if m.group(2) == "TB" else 1)) if m else 10**9
    term = min(terms, key=gb) if terms else None
    if term:
        sslug = term.get("slug")
        storage = smap.get(sslug) or normalize_storage(term.get("name"))
    else:
        sslug, storage = None, None
    if not storage:
        return []
    badge = (prod.get("stock_availability") or {}).get("class", "")
    in_stock = bool(prod.get("is_purchasable")) and badge == "in-stock"
    return [{"storage": storage, "storage_slug": sslug, "color_slug": None,
             "price": price, "in_stock": in_stock}]


def variations_for(prod, smap):
    attrs = {a.get("taxonomy"): a for a in (prod.get("attributes") or [])}
    storage_terms = (attrs.get("pa_storage") or {}).get("terms") or []
    color_terms = (attrs.get("pa_color") or {}).get("terms") or []
    is_variable = prod.get("type") == "variable"

    need_matrix = is_variable and (len(storage_terms) > 1 or INCLUDE_OOS)
    if need_matrix:
        try:
            page = requests.get(f"{BASE_URL}/product/{prod.get('slug','')}/",
                                headers=HEADERS, timeout=30).text
        except Exception:
            page = ""
        embedded = parse_embedded(page, smap)
        if embedded is None and storage_terms:  # no form -> enumerate via ajax
            embedded = ajax_matrix(prod.get("id"), storage_terms, color_terms, smap)
        if embedded and _consistent(embedded, prod):
            return embedded
        # embedded missing or inconsistent with the advertised price -> fall back

    return _store_api_single(prod, smap)


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)

    products = fetch_listing()
    print(f"Fetched {len(products)} products in category {CATEGORY_ID}")

    best = {}  # (variant_key, condition) -> lowest-price offer

    for prod in products:
        model = model_from_name(prod.get("name", ""))
        if not model or not is_phone(model):
            continue

        badge = (prod.get("stock_availability") or {}).get("class", "")
        product_in_stock = bool(prod.get("is_purchasable")) and badge == "in-stock"
        if not product_in_stock and not INCLUDE_OOS:
            continue

        condition = condition_for(prod)
        permalink = prod.get("permalink") or f"{BASE_URL}/product/{prod.get('slug','')}/"
        images = prod.get("images") or []
        prod_img = images[0].get("src") if images else None
        smap = storage_map(prod)

        for v in variations_for(prod, smap):
            if not v["in_stock"] and not INCLUDE_OOS:
                continue
            storage, price = v["storage"], v["price"]
            vkey = make_variant_key(model, storage, None)
            availability = "in_stock" if v["in_stock"] else "out_of_stock"
            bkey = (vkey, condition)
            if not better_offer(availability, price, best.get(bkey)):
                continue

            params = []
            if v.get("storage_slug"):
                params.append(f"attribute_pa_storage={v['storage_slug']}")
            if v.get("color_slug"):
                params.append(f"attribute_pa_color={v['color_slug']}")
            url = f"{permalink}?{'&'.join(params)}" if params else permalink

            best[bkey] = {
                "model": model, "storage": storage, "ram": None,
                "variant_key": vkey, "condition": condition,
                "price": price, "availability": availability,
                "url": url, "image_url": prod_img,
                "name": f"{model} {storage}".strip(),
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
            condition=o["condition"], url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:34} [{o['condition']:18}] ₹{o['price']:.0f}  [{o['availability']}]")

    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=bool(best))
    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
