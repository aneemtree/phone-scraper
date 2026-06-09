"""
One-off probe: find where (and whether) each store exposes WARRANTY in its
source, so we can decide per-store between per-offer extraction vs a store-level
default. Read-only, no DB writes, no repo imports. Run and paste the output back.

    python3 probe_warranty.py

For each store it fetches a small sample and reports every distinct
warranty-related snippet it finds (with a count), plus where it lived
(title / tags / body_html / option / variant-title / api-field).
"""
import re
import json
import time
import requests
from collections import Counter

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "application/json,text/html,*/*"}
TIMEOUT = 30

# capture "<n> month(s)/year(s) warranty" and "warranty ... <n> month/year"
WARR_RE = re.compile(r"(\d+)\s*(month|year)s?\b[^.<\n]{0,30}warranty|warranty[^.<\n]{0,30}?(\d+)\s*(month|year)s?", re.I)
HAS_WARR = re.compile(r"warrant", re.I)


def snippets(text):
    """Return short warranty snippets found in a blob of text/HTML."""
    if not text:
        return []
    out = []
    for m in re.finditer(r".{0,25}warrant.{0,35}", str(text), re.I):
        s = re.sub(r"<[^>]+>", " ", m.group(0))
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            out.append(s)
    return out


def report(store, found, sampled, note=""):
    print(f"\n=== {store}  (sampled {sampled} products){'  '+note if note else ''} ===")
    if not found:
        print("  NO warranty signal found in sample")
        return
    for snip, cnt in found.most_common(25):
        print(f"  [{cnt:4}x] {snip[:90]}")


def probe_shopify(store, url_tmpl, pages=2):
    """url_tmpl has {page}. Scan title/tags/body_html/options/variant-titles."""
    found = Counter()
    sampled = 0
    try:
        for page in range(1, pages + 1):
            r = requests.get(url_tmpl.format(page=page), headers=H, timeout=TIMEOUT)
            if r.status_code != 200:
                report(store, found, sampled, note=f"HTTP {r.status_code}")
                return
            prods = r.json().get("products", [])
            if not prods:
                break
            for p in prods:
                sampled += 1
                blobs = [p.get("title", ""), p.get("body_html", "")]
                blobs += [str(t) for t in p.get("tags", []) if isinstance(p.get("tags"), list)]
                for o in p.get("options", []):
                    blobs.append(f"OPT[{o.get('name')}]: " + " | ".join(map(str, o.get("values", []))))
                for v in p.get("variants", []):
                    blobs.append("VAR: " + str(v.get("title", "")))
                for b in blobs:
                    if HAS_WARR.search(str(b)):
                        for s in snippets(b):
                            found[s] += 1
            if len(prods) < 200:
                break
            time.sleep(0.3)
        report(store, found, sampled)
    except Exception as e:
        print(f"\n=== {store} === ERROR: {e}")


def probe_woo(store, base, cat, per=40):
    """WooCommerce Store API: scan name/description/short_description/attributes."""
    found = Counter()
    sampled = 0
    try:
        url = f"{base}/wp-json/wc/store/v1/products?category={cat}&per_page={per}&page=1"
        r = requests.get(url, headers=H, timeout=TIMEOUT)
        if r.status_code != 200:
            report(store, found, sampled, note=f"HTTP {r.status_code}")
            return
        for p in r.json():
            sampled += 1
            blobs = [p.get("name", ""), p.get("description", ""), p.get("short_description", "")]
            for a in p.get("attributes", []):
                blobs.append(f"ATTR[{a.get('name')}]: " + " | ".join(
                    str(t.get("name")) for t in a.get("terms", [])))
            for b in blobs:
                if HAS_WARR.search(str(b)):
                    for s in snippets(b):
                        found[s] += 1
        report(store, found, sampled)
    except Exception as e:
        print(f"\n=== {store} === ERROR: {e}")


def probe_gadgetrebirth():
    store = "gadgetrebirth"
    found = Counter()
    sampled = 0
    try:
        r = requests.get("https://api.gadgetrebirth.com/api/products?limit=100&skip=0",
                         headers=H, timeout=TIMEOUT)
        if r.status_code != 200:
            report(store, found, sampled, note=f"HTTP {r.status_code}")
            return
        data = r.json()
        prods = data if isinstance(data, list) else data.get("products", data.get("data", []))
        # also dump the KEYS available on a product + a variant, so we can spot a warranty field
        if prods:
            print(f"\n--- {store}: product keys = {sorted(prods[0].keys())}")
            v = (prods[0].get("variants") or [{}])[0]
            print(f"--- {store}: variant keys = {sorted(v.keys())}")
        for p in prods:
            sampled += 1
            blob = json.dumps(p)
            if HAS_WARR.search(blob):
                for s in snippets(blob):
                    found[s] += 1
        report(store, found, sampled)
    except Exception as e:
        print(f"\n=== {store} === ERROR: {e}")


def probe_sahivalue():
    # Zoho commerce — just check the homepage/policy text for a blanket warranty
    store = "sahivalue"
    try:
        r = requests.get("https://www.sahivalue.com", headers=H, timeout=TIMEOUT)
        snip = Counter()
        for s in snippets(r.text):
            snip[s] += 1
        report(store, snip, 1, note="(homepage text only)")
    except Exception as e:
        print(f"\n=== {store} === ERROR: {e}")


if __name__ == "__main__":
    probe_shopify("refit",      "https://refitglobal.com/collections/refurbished-mobiles/products.json?limit=250&page={page}")
    probe_shopify("oldsold",    "https://oldsold.in/products.json?limit=250&page={page}")
    probe_shopify("grest",      "https://grest.in/collections/iphones/products.json?limit=250&page={page}")
    probe_shopify("tetro",      "https://tetro.in/collections/all/products.json?limit=250&page={page}")
    probe_shopify("easyphones", "https://easyphones.co.in/collections/all-collection/products.json?limit=250&page={page}")
    probe_shopify("budli",      "https://buy.budli.in/collections/mobile-phones/products.json?limit=250&page={page}")
    probe_shopify("mobilegoo-mobiles", "https://mobilegoo.shop/collections/mobiles/products.json?limit=250&page={page}")
    probe_shopify("mobilegoo-unbox",   "https://mobilegoo.shop/collections/unbox-mobiles/products.json?limit=250&page={page}")
    probe_shopify("maplestore", "https://maplestore.in/collections/all-iphones/products.json?limit=250&page={page}")

    probe_woo("thephonehub", "https://thephonehub.in", 160)
    probe_woo("cellbuddy",   "https://cellbuddy.in/buddy", 94)
    probe_woo("itradeit-openbox",   "https://itradeit.in", 438)
    probe_woo("itradeit-refurb",    "https://itradeit.in", 60)

    probe_gadgetrebirth()
    probe_sahivalue()
    print("\n\nDONE. Paste everything above back.")
