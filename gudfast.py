"""
GudFast scraper (gudfast.com) — refurbished phones (+ watches/laptops, filtered out).

WooCommerce on WordPress. Requests-only (no browser):
  - Listing + metadata from the public Store API
    (/wp-json/wc/store/v1/products?category=123 — "Refurbished Smartphone", the
    master phone category; brand categories like Apple(102) mix in watches, so we
    use 123 and still drop non-phones via is_phone()).
  - Two product shapes:
      * VARIABLE: axes are Condition (pa_condition: Good/Superb) and optionally
        Color (pa_color). Storage is NOT an attribute — it's in the TITLE. The
        full variation matrix is INLINED on the product page as
        `data-product_variations="<escaped JSON>"` (no wc-ajax needed); each
        variation carries `display_price` (already rupees) + `is_in_stock`. One
        row per (storage, condition) at the LOWEST price across colors.
      * SIMPLE: condition + storage are baked into the TITLE ("… 128GB (Black) –
        Good Condition | 1 Month Warranty …"); price is the Store API price
        (minor units, ÷100). One row.

Storage always comes from the title (largest GB/TB token); there is no RAM axis.
Conditions Good/Superb share the Cashify vocab; an ungraded product falls back to
"Unknown Condition" via normalize_condition("Refurbished").

Availability (source of truth): per-variation is_in_stock/is_purchasable for
variable products; is_in_stock + is_purchasable for simple. Reviews: the Woo
Store API exposes native per-product average_rating/review_count — stored only
when review_count > 0 (genuine per-product reviews). Warranty is a blanket
"1 Month Warranty" advertised store-wide -> store default_warranty_days (set via
SQL, suggested 30); not captured per offer.

Deep-link: permalink + ?attribute_pa_condition=&attribute_pa_color=.

Run with: python3 gudfast.py
"""
import re
import html
import time
import json
import sys
import requests
from normalize import clean_model, normalize_storage, make_variant_key, normalize_condition, is_phone, normalize_ram
from obs import init_sentry, log_error
# db is imported lazily inside scrape() so build_offers()/--dry stay import-free
# (no httpx/supabase needed just to validate parsing).

SITE = "gudfast"
BASE_URL = "https://gudfast.com"
CATEGORY_ID = 123  # "Refurbished Smartphone" (master phone category)
DELAY = 0.3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}

_STORAGE_RE = re.compile(r"(\d+)\s*(GB|TB)\b", re.I)
# Condition words that may appear in a simple product's title.
_TITLE_COND_RE = re.compile(r"\b(Superb|Good|Fair|Excellent|Like\s*New|Brand\s*New|Open\s*Box)\b", re.I)


def storage_from_title(name):
    """Largest GB/TB token in the title -> normalized storage (e.g. '128gb')."""
    best = None
    for num, unit in _STORAGE_RE.findall(html.unescape(name or "")):
        gb = int(num) * (1024 if unit.lower() == "tb" else 1)
        if best is None or gb > best[0]:
            best = (gb, f"{num}{unit.upper()}")
    return normalize_storage(best[1]) if best else None


def ram_from_title(name, permalink=""):
    """GudFast bakes the RAM into the title/slug ("… 256 GB, 8 GB RAM …" /
    "…-256-gb-8-gb-ram-refurbished"), so capture it explicitly (the storage is
    the largest GB token; RAM is the one labelled 'RAM'). Try the title first,
    then the permalink slug (dashes -> spaces). Returns None when no RAM is
    labelled — never guesses from the storage token (normalize_ram requires the
    word 'RAM')."""
    return (normalize_ram(html.unescape(name or ""))
            or normalize_ram((permalink or "").replace("-", " ")))


def ram_from_desc(short_html):
    """Fallback: GudFast's short_description highlight line carries the RAM even
    when the title/slug don't ("8 GB RAM | 256 GB ROM | ..."). HTML stripped; a
    slash RANGE stays None. normalize_ram requires the word RAM, so the "256 GB
    ROM" storage token is never misread as RAM."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(short_html or ""))
    if re.search(r"\d+\s*GB\s*/\s*\d+\s*GB\s*RAM", text, re.I):
        return None
    return normalize_ram(text)


def build_model(name):
    """Clean a GudFast title into a model name. Titles carry a marketing tail
    ('… – Good Condition | 1 Month Warranty | 5 Days Replacement') and bracketed
    storage/colour/'Refurbished' chunks; strip those, then clean_model handles
    the rest (brand casing, leftover storage/colour tokens)."""
    s = html.unescape(name or "")
    s = s.split("|", 1)[0]                 # drop warranty/replacement tail
    s = re.split(r"[–—-]\s", s, 1)[0]      # drop "– Good Condition" tail
    s = re.sub(r"\([^)]*\)", " ", s)       # drop (128 GB) / (Black) / (Refurbished)
    return clean_model(s)


def title_condition(name):
    """Condition for a SIMPLE product, parsed from the title; default Unknown."""
    m = _TITLE_COND_RE.search(html.unescape(name or ""))
    return normalize_condition(m.group(1)) if m else normalize_condition("Refurbished")


def fetch_listing():
    """All products in the refurbished-smartphone category (paginated)."""
    products, page = [], 1
    while True:
        url = (f"{BASE_URL}/wp-json/wc/store/v1/products"
               f"?category={CATEGORY_ID}&per_page=100&page={page}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except Exception:
            break
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


def fetch_page(slug):
    try:
        return requests.get(f"{BASE_URL}/product/{slug}/", headers=HEADERS, timeout=30).text
    except Exception:
        return ""


def parse_variations(page):
    """Variable product's inlined variation matrix -> list of
    {cond_slug, color_slug, price (rupees), in_stock}. None if absent/unparseable."""
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
        a = v.get("attributes") or {}
        price = v.get("display_price")
        if price is None:
            continue
        in_stock = bool(v.get("is_in_stock") and v.get("is_purchasable")
                        and v.get("variation_is_active", True))
        out.append({
            "cond_slug": a.get("attribute_pa_condition"),
            "color_slug": a.get("attribute_pa_color"),
            "price": float(price),
            "in_stock": in_stock,
        })
    return out


def entries_for(prod):
    """Yield offer entries (condition, price, availability, cond_slug, color_slug)
    for a product, reading the variation matrix for variable products and the
    Store API price for simple ones."""
    if prod.get("type") == "variable":
        vs = parse_variations(fetch_page(prod.get("slug", "")))
        if vs is None:
            return []
        return [{
            "condition": normalize_condition((v["cond_slug"] or "Refurbished").replace("-", " ")),
            "price": v["price"],
            "availability": "in_stock" if v["in_stock"] else "out_of_stock",
            "cond_slug": v["cond_slug"], "color_slug": v["color_slug"],
        } for v in vs]

    # Simple product: one offer from the Store API price + title condition.
    price_minor = (prod.get("prices") or {}).get("price")
    try:
        price = float(price_minor) / 100.0
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return []
    in_stock = bool(prod.get("is_in_stock") and prod.get("is_purchasable"))
    return [{
        "condition": title_condition(prod.get("name", "")),
        "price": price,
        "availability": "in_stock" if in_stock else "out_of_stock",
        "cond_slug": None, "color_slug": None,
    }]


def _better(availability, price, cur):
    """Local better-offer (in_stock beats out_of_stock; else lower price) so
    build_offers stays import-free for --dry. Mirrors db.better_offer."""
    if cur is None:
        return True
    if availability == "in_stock" and cur["availability"] != "in_stock":
        return True
    if availability != "in_stock" and cur["availability"] == "in_stock":
        return False
    return price < cur["price"]


def build_offers(products, include_oos=False):
    """Pure: products -> { (variant_key, condition): offer }. No DB."""
    best = {}
    for prod in products:
        raw_name = prod.get("name", "")
        model = build_model(raw_name)
        if not model or not is_phone(model):
            continue  # drops watches/laptops/accessories that slip into the category
        storage = storage_from_title(raw_name)
        if not storage:
            continue  # no storage token (e.g. a watch) -> not a phone listing

        vkey = make_variant_key(model, storage, None)
        permalink = prod.get("permalink") or f"{BASE_URL}/product/{prod.get('slug','')}/"
        ram = ram_from_title(raw_name, permalink) or ram_from_desc(prod.get("short_description"))
        images = prod.get("images") or []
        prod_img = images[0].get("src") if images else None
        # Native Woo per-product reviews (stored only when there's at least one).
        try:
            review_count = int(prod.get("review_count") or 0)
        except (TypeError, ValueError):
            review_count = 0
        try:
            rating = float(prod.get("average_rating")) if review_count > 0 else None
        except (TypeError, ValueError):
            rating = None

        for e in entries_for(prod):
            if e["availability"] != "in_stock" and not include_oos:
                continue
            bkey = (vkey, e["condition"])
            if not _better(e["availability"], e["price"], best.get(bkey)):
                continue

            params = []
            if e.get("cond_slug"):
                params.append(f"attribute_pa_condition={e['cond_slug']}")
            if e.get("color_slug"):
                params.append(f"attribute_pa_color={e['color_slug']}")
            url = f"{permalink}?{'&'.join(params)}" if params else permalink

            best[bkey] = {
                "model": model, "storage": storage, "variant_key": vkey, "ram": ram,
                "condition": e["condition"], "price": e["price"],
                "availability": e["availability"], "url": url, "image_url": prod_img,
                "rating": rating, "review_count": review_count if review_count > 0 else None,
                "name": f"{model} {storage}".strip(),
            }
    return best


def scrape():
    from datetime import datetime, timezone
    from db import (save_phone, save_price, ensure_image, mark_site_oos,
                    mark_unseen_out_of_stock, INCLUDE_OOS)
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)

    products = fetch_listing()
    print(f"Fetched {len(products)} products in category {CATEGORY_ID}")
    best = build_offers(products, include_oos=INCLUDE_OOS)
    print(f"Unique (variant, condition) offers: {len(best)}")

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
            o["model"], o["storage"], o.get("ram"), o["variant_key"],
            in_stock=(o["name"] in in_stock_names),
        )
        save_price(
            pid, o["price"], availability=o["availability"], condition=o["condition"],
            url=o["url"], rating=o.get("rating"), review_count=o.get("review_count"),
        )
        saved += 1
        print(f"  saved: {o['name']:42} [{o['condition']:12}] ₹{o['price']:.0f}  [{o['availability']}]")

    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=bool(best))
    print(f"\nDone. Saved {saved} offers from {SITE}.")


def dry_run():
    """Fetch + build + print offers with NO DB (validate parsing)."""
    products = fetch_listing()
    print(f"Fetched {len(products)} products in category {CATEGORY_ID}")
    best = build_offers(products, include_oos="--oos" in sys.argv)
    print(f"Unique (variant, condition) offers: {len(best)}\n")
    for o in sorted(best.values(), key=lambda x: (x["model"], x["storage"], x["condition"])):
        print(f"  {o['name']:42} [{o['condition']:12}] ₹{o['price']:.0f}  [{o['availability']}]  {o['url']}")


if __name__ == "__main__":
    if "--dry" in sys.argv:
        dry_run()
    else:
        init_sentry(SITE)
        try:
            scrape()
        except Exception as e:
            log_error(e, site=SITE, phase="scrape")
            raise
