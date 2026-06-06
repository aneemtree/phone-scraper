"""
ThePhoneHub scraper (thephonehub.in) — refurbished phones.

WooCommerce store on WordPress. Requests-only (no browser):
  - Listing + product metadata come from the public WooCommerce Store API
    (/wp-json/wc/store/v1/products?category=160 — "Refurbished Smartphones").
  - Per-variant price/stock/grade come from the variation matrix. Most products
    embed it in the product page as `data-product_variations="<escaped JSON>"`.
    Above WooCommerce's ajax threshold the attribute is the string "False"
    instead — for those we enumerate combinations via `?wc-ajax=get_variation`.
    A few single-variant products embed no form at all; those fall back to the
    Store API price + storage parsed from the title.

Conditions: the store uses Fair/Good/Superb grades (pa_grade) on SOME products
and none on others. Where a grade exists we save one row per (storage, grade);
otherwise condition is "Refurbished". For each (storage, grade) we keep the
LOWEST price across colors, available variants only.

Availability (source of truth): the rendered buy state — product `is_purchasable`
plus the `stock_availability` badge, and per-variation `is_in_stock` /
`is_purchasable`. The top-level `is_in_stock` flag is phantom here (always true)
so it is NOT trusted, per the standard availability rule.

Prices: variation `display_price` is already rupees; Store API `prices.price` is
minor units (÷100). OOS products report Store API price 0, so OOS prices must
come from the variation matrix.

Deep-link: permalink + ?attribute_pa_storage=&attribute_pa_grade=&attribute_pa_color=.

Run with: python3 thephonehub.py
"""
import re
import html
import time
import json
import itertools
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


def title_to_model(name):
    """Strip the marketing tail and clean a product title into a model name.
    The model is always the text before the first '(' (storage is parenthesised)
    or, failing that, before the first dash/pipe separator."""
    name = html.unescape(name or "")
    if "(" in name:
        head = name.split("(", 1)[0]
    else:
        head = re.split(r"[–—|]", name, maxsplit=1)[0]
    return clean_model(head)


def storage_slug_to_size(slug):
    """'12gb-128gb' / '256-gb' -> (ram, storage) via the shared size parser.
    The pa_storage slug joins ram and storage with '-'; some are storage-only."""
    if not slug:
        return None, None
    # "256-gb" -> "256gb"; "12gb-128gb" -> "12gb|128gb" for parse_size_string.
    s = re.sub(r"(\d+)-(gb|tb)\b", r"\1\2", slug, flags=re.I)
    return parse_size_string(s.replace("-", "|"))


def ram_storage_from_title(name):
    """Best-effort (ram, storage) from a title for products with no pa_storage
    attribute (e.g. 'Vivo T1 Pro … 8GB RAM, 128GB Storage')."""
    toks = re.findall(r"\d+\s*(?:GB|TB)", html.unescape(name or ""), re.I)
    if not toks:
        return None, None
    if len(toks) == 1:
        return None, normalize_storage(toks[0])
    return parse_size_string("|".join(toks[:2]))


def grade_to_condition(grade_slug):
    """'superb' -> 'Superb', 'like-new' -> 'Like New', None -> 'Refurbished'."""
    if not grade_slug:
        return normalize_condition("Refurbished")
    return normalize_condition(grade_slug.replace("-", " "))


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


def _variation_entry(storage_slug, grade_slug, color_slug, price, in_stock):
    ram, storage = storage_slug_to_size(storage_slug)
    if not storage or not price:
        return None
    return {
        "storage_slug": storage_slug, "grade_slug": grade_slug,
        "color_slug": color_slug, "ram": ram, "storage": storage,
        "price": float(price), "in_stock": bool(in_stock),
    }


def parse_embedded(page):
    """Return the embedded variation list, or None if the page has no usable
    form (no attribute, or WooCommerce emitted the string "False" above its
    ajax-variation threshold)."""
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
        in_stock = bool(v.get("is_in_stock") and v.get("is_purchasable")
                        and v.get("variation_is_active"))
        e = _variation_entry(
            attrs.get("attribute_pa_storage"), attrs.get("attribute_pa_grade"),
            attrs.get("attribute_pa_color"), v.get("display_price"), in_stock)
        if e:
            out.append(e)
    return out


def ajax_matrix(product_id, storage_terms, grade_terms, color_terms):
    """Enumerate storage×grade×color via ?wc-ajax=get_variation for products that
    don't embed the matrix. Each axis with no terms contributes a single None."""
    axes = [
        [(t.get("slug")) for t in storage_terms] or [None],
        [(t.get("slug")) for t in grade_terms] or [None],
        [(t.get("slug")) for t in color_terms] or [None],
    ]
    out = []
    for storage_slug, grade_slug, color_slug in itertools.product(*axes):
        data = {"product_id": str(product_id)}
        if storage_slug:
            data["attribute_pa_storage"] = storage_slug
        if grade_slug:
            data["attribute_pa_grade"] = grade_slug
        if color_slug:
            data["attribute_pa_color"] = color_slug
        try:
            r = requests.post(f"{BASE_URL}/?wc-ajax=get_variation",
                              headers=HEADERS, data=data, timeout=30)
            d = r.json()
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("variation_id") or d.get("display_price") is None:
            continue  # combination doesn't exist
        in_stock = bool(d.get("is_in_stock") and d.get("is_purchasable"))
        e = _variation_entry(storage_slug, grade_slug, color_slug,
                             d.get("display_price"), in_stock)
        if e:
            out.append(e)
        time.sleep(DELAY)
    return out


def fetch_page(slug):
    try:
        return requests.get(f"{BASE_URL}/product/{slug}/", headers=HEADERS, timeout=30).text
    except Exception:
        return ""


def variations_for(prod):
    """Resolve the full (storage, grade, color, price, stock) matrix for a
    product, choosing the cheapest reliable source. Returns a list of variation
    entries (possibly empty)."""
    attrs = {a.get("taxonomy"): a for a in (prod.get("attributes") or [])}
    storage_terms = (attrs.get("pa_storage") or {}).get("terms") or []
    grade_terms = (attrs.get("pa_grade") or {}).get("terms") or []
    color_terms = (attrs.get("pa_color") or {}).get("terms") or []
    is_variable = prod.get("type") == "variable"
    has_grade = bool(grade_terms)

    # We must read the matrix when grades exist (per-grade price), when there are
    # multiple storages (per-storage price), or for the OOS catalog (Store API
    # price is 0 when sold out). Single-storage no-grade products are fine on the
    # Store API min price alone.
    need_matrix = is_variable and (has_grade or len(storage_terms) > 1 or INCLUDE_OOS)
    if need_matrix:
        embedded = parse_embedded(fetch_page(prod.get("slug", "")))
        if embedded is not None:
            return embedded
        if storage_terms:  # not embedded but enumerable via ajax
            return ajax_matrix(prod.get("id"), storage_terms, grade_terms, color_terms)

    # Fallback: a single offer from the Store API min price.
    price_minor = (prod.get("prices") or {}).get("price")
    try:
        price = float(price_minor) / 100.0
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return []
    if storage_terms:
        storage_slug = storage_terms[0].get("slug")
        ram, storage = storage_slug_to_size(storage_slug)
    else:
        storage_slug = None
        ram, storage = ram_storage_from_title(prod.get("name", ""))
    if not storage:
        return []
    grade_slug = grade_terms[0].get("slug") if len(grade_terms) == 1 else None
    badge = (prod.get("stock_availability") or {}).get("class", "")
    in_stock = bool(prod.get("is_purchasable")) and badge == "in-stock"
    return [{
        "storage_slug": storage_slug, "grade_slug": grade_slug, "color_slug": None,
        "ram": ram, "storage": storage, "price": price, "in_stock": in_stock,
    }]


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
        # Filter on the CLEAN model, not the slug: product slugs embed marketing
        # words ("50mp-ois-camera") that collide with the non-phone keyword list.
        if not model or not is_phone(model):
            continue

        badge = (prod.get("stock_availability") or {}).get("class", "")
        product_in_stock = bool(prod.get("is_purchasable")) and badge == "in-stock"
        if not product_in_stock and not INCLUDE_OOS:
            continue

        permalink = prod.get("permalink") or f"{BASE_URL}/product/{prod.get('slug','')}/"
        images = prod.get("images") or []
        prod_img = images[0].get("src") if images else None

        for v in variations_for(prod):
            if not v["in_stock"] and not INCLUDE_OOS:
                continue
            storage, ram, price = v["storage"], v["ram"], v["price"]
            condition = grade_to_condition(v.get("grade_slug"))
            vkey = make_variant_key(model, storage, ram)
            availability = "in_stock" if v["in_stock"] else "out_of_stock"
            bkey = (vkey, ram, condition)
            if not better_offer(availability, price, best.get(bkey)):
                continue

            params = []
            for key in ("storage", "grade", "color"):
                slug = v.get(f"{key}_slug")
                if slug:
                    params.append(f"attribute_pa_{key}={slug}")
            url = f"{permalink}?{'&'.join(params)}" if params else permalink

            best[bkey] = {
                "model": model, "storage": storage, "ram": ram,
                "variant_key": vkey, "condition": condition,
                "price": price, "availability": availability,
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
            condition=o["condition"], url=o["url"],
        )
        saved += 1
        print(f"  saved: {o['name']:42} [{o['condition']:12}] ₹{o['price']:.0f}  [{o['availability']}]")

    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=bool(best))
    print(f"\nDone. Saved {saved} offers from {SITE}.")


if __name__ == "__main__":
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise
