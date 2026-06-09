#!/usr/bin/env python3
"""Read-only probe: where does each store expose product reviews, and how hard
to fetch? Run it locally (the CI/sandbox can't reach the stores) and paste the
output back. No DB writes; mirrors probe_warranty.py.

It checks three reachable signals per store:
  - WooCommerce Store API: products carry native `average_rating`/`review_count`.
  - Shopify product page: which review app is embedded (Judge.me/Loox/Yotpo/...)
    and whether JSON-LD `aggregateRating` / a Judge.me badge is in the HTML.
  - gadgetrebirth JSON API: any rating/review keys on the product object.

Usage:  python3 probe_reviews.py
"""
import json, re, sys
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
S = requests.Session(); S.headers.update(UA)

def jget(url):
    r = S.get(url, timeout=30); r.raise_for_status(); return r.json()
def tget(url):
    r = S.get(url, timeout=30); r.raise_for_status(); return r.text

REVIEW_APPS = {
    "judge.me": ["judge.me", "jdgm-", "data-average-rating"],
    "loox":     ["loox.io", "loox-rating", "data-loox"],
    "yotpo":    ["yotpo", "yotpo-main-widget"],
    "stamped":  ["stamped.io", "stamped-main-widget"],
    "okendo":   ["okendo", "oke-stars"],
    "reviews.io": ["reviews.io", "ruk_rating"],
    "shopify_spr": ["shopify-product-reviews", "spr-badge", "spr-summary"],
    "opinew":   ["opinew"],
    "fera":     ["fera.ai", "fera-"],
}

def detect_apps(html):
    low = html.lower()
    return [name for name, sigs in REVIEW_APPS.items() if any(s in low for s in sigs)]

def jsonld_rating(html):
    rv = re.search(r'"ratingValue"\s*:\s*"?([0-9.]+)"?', html)
    rc = re.search(r'"reviewCount"\s*:\s*"?([0-9]+)"?', html)
    return (rv.group(1) if rv else None, rc.group(1) if rc else None)

def jdgm_badge(html):
    avg = re.search(r'data-average-rating=["\']([0-9.]+)["\']', html)
    num = re.search(r'data-number-of-reviews=["\']([0-9]+)["\']', html)
    return (avg.group(1) if avg else None, num.group(1) if num else None)

def woo(name, base, cats):
    """WooCommerce Store API: native average_rating + review_count."""
    cats = cats if isinstance(cats, (list, tuple)) else [cats]
    seen = rated = 0; samples = []
    for c in cats:
        try:
            data = jget(f"{base}/wp-json/wc/store/v1/products?category={c}&per_page=30")
        except Exception as e:
            print(f"[{name}] woo cat={c} ERR {type(e).__name__}: {e}"); continue
        for p in data:
            seen += 1
            ar = float(p.get("average_rating") or 0)
            rc = int(p.get("review_count") or 0)
            if ar > 0 or rc > 0:
                rated += 1
                if len(samples) < 5:
                    samples.append((p.get("name", "")[:30], ar, rc))
    print(f"[{name}] WooCommerce Store API: {rated}/{seen} sampled products have reviews")
    for s in samples: print("      ", s)

def shopify(name, base, coll=""):
    """Detect review app + aggregate rating on one product page."""
    try:
        purl = f"{base}{coll}/products.json?limit=1" if coll else f"{base}/products.json?limit=1"
        prods = jget(purl).get("products", [])
        if not prods:
            print(f"[{name}] shopify: no products"); return
        handle = prods[0]["handle"]
        html = tget(f"{base}/products/{handle}")
    except Exception as e:
        print(f"[{name}] shopify ERR {type(e).__name__}: {e}"); return
    apps = detect_apps(html)
    rv, rc = jsonld_rating(html)
    javg, jnum = jdgm_badge(html)
    print(f"[{name}] shopify handle={handle}")
    print(f"      apps={apps or 'none'}  jsonld(rating,count)=({rv},{rc})  jdgm_badge(avg,num)=({javg},{jnum})")

def gadgetrebirth():
    try:
        data = jget("https://api.gadgetrebirth.com/api/products?limit=5&skip=0").get("products", [])
    except Exception as e:
        print(f"[gadgetrebirth] ERR {type(e).__name__}: {e}"); return
    if not data:
        print("[gadgetrebirth] no products"); return
    keys = sorted(data[0].keys())
    rk = [k for k in keys if re.search(r"rat|review|star", k, re.I)]
    print(f"[gadgetrebirth] product keys with rating/review: {rk or 'NONE'}")
    if rk:
        for p in data[:5]:
            print("      ", p.get("name", "")[:30], {k: p.get(k) for k in rk})

def generic(name, url):
    """Fetch a custom storefront page and look for app + aggregateRating."""
    try:
        html = tget(url)
    except Exception as e:
        print(f"[{name}] ERR {type(e).__name__}: {e}"); return
    rv, rc = jsonld_rating(html)
    print(f"[{name}] {url}\n      apps={detect_apps(html) or 'none'}  jsonld(rating,count)=({rv},{rc})")

if __name__ == "__main__":
    print("=== WooCommerce (native rating fields — easiest) ===")
    woo("thephonehub", "https://thephonehub.in", 160)
    woo("cellbuddy",   "https://cellbuddy.in/buddy", 94)
    woo("itradeit",    "https://itradeit.in", [438, 60])

    print("\n=== Shopify (review app on product page) ===")
    shopify("refit",      "https://refitglobal.com", "/collections/refurbished-mobiles")
    shopify("oldsold",    "https://oldsold.in")
    shopify("easyphones", "https://easyphones.co.in", "/collections/all-collection")
    shopify("tetro",      "https://tetro.in", "/collections/all")
    shopify("grest",      "https://grest.in", "/collections/iphones")
    shopify("budli",      "https://buy.budli.in", "/collections/mobile-phones")
    shopify("maplestore", "https://maplestore.in", "/collections/all-iphones")
    shopify("mobilegoo",  "https://mobilegoo.shop", "/collections/mobiles")

    print("\n=== Custom storefronts ===")
    gadgetrebirth()
    generic("sahivalue", "https://www.sahivalue.com")

    print("\nDone. Paste this output back.")
