"""
Samsung Certified Re-Newed (samsung.com/in/certified-re-newed) — requests-only.

Samsung's official OEM-renewed Galaxy program (1-year Samsung warranty). The
landing page is JS-rendered, but it embeds the representative SKU codes
(SM5...INS, one per family) and the product data comes from a public JSON API:
  searchapi.samsung.com/v6/front/b2c/product/model/list/newhybris/cheil
    ?siteCode=in&modelList=<csv codes>&saleSkuYN=N&onlyRequestSkuYN=N&commonCodeYN=N
which returns every family + all its colour / storage / RAM variants.

Per variant (productList[].modelList[]): displayName ("Galaxy S25 Ultra Certified
Re-Newed"), storage + colour via fmyChipList, RAM via the displayName "(12 GB
Memory)" suffix, `price` (strike) + `promotionPrice` (actual selling price, what
we save), `stockStatusText` in/outOfStock (the validated availability flag),
per-PRODUCT `ratings`/`reviewCount` (genuine Samsung reviews), `pdpUrl`, `largeUrl`.

Condition: single "Certified Re-Newed". Warranty: 1-year Samsung -> "Brand
Warranty" label. RAM matters (Galaxy A56 ships as separate 8 GB / 12 GB families),
so — like oldsold/itradeit — RAM is folded into the saved `name` and the dedup
key (variant_key, ram, condition); make_variant_key stays storage-only so the
phone still groups cross-store. clean_model maps "Galaxy …" -> "Samsung Galaxy …"
so the key matches the other stores.

Run: python3 samsungcr.py        (scrape + save)
     python3 samsungcr.py --dry  (fetch + parse + print, NO DB)
"""
import re
import os
import sys
import requests
from normalize import clean_model, normalize_storage, make_variant_key, is_phone
from obs import init_sentry, log_error

SITE = "samsungcr"
LANDING = "https://www.samsung.com/in/certified-re-newed/"
API = "https://searchapi.samsung.com/v6/front/b2c/product/model/list/newhybris/cheil"
CONDITION = "Certified Re-Newed"
WARRANTY_LABEL = "Brand Warranty"          # 1-year Samsung manufacturer warranty
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}


def fetch_codes():
    """Representative SKU codes (one per family) embedded in the landing page."""
    r = requests.get(LANDING, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return sorted(set(re.findall(r"SM5[A-Z0-9]{6,}INS", r.text)))


def fetch_families(codes):
    params = {"siteCode": "in", "modelList": ",".join(codes), "saleSkuYN": "N",
              "onlyRequestSkuYN": "N", "commonCodeYN": "N"}
    r = requests.get(API, headers={**HEADERS, "Referer": LANDING}, params=params, timeout=30)
    r.raise_for_status()
    return (r.json().get("response", {}).get("resultData", {}).get("productList", []) or [])


def _chip(model, chip_type):
    for c in (model.get("fmyChipList") or []):
        if c.get("fmyChipType") == chip_type:
            return c.get("fmyChipName") or c.get("fmyChipLocalName")
    return None


def _clean_name(display, fallback=""):
    s = display or fallback or ""
    s = re.sub(r"\([^)]*memory[^)]*\)", " ", s, flags=re.I)   # drop "(12 GB Memory)"
    s = re.sub(r"certified\s*re-?newed", " ", s, flags=re.I)  # hyphenated, clean_model misses it
    return clean_model(s)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _img(u):
    if not u:
        return None
    return ("https:" + u) if u.startswith("//") else u


# Inlined so --dry needs no db import / Supabase env (mirrors db.better_offer +
# db.INCLUDE_OOS). scrape() still uses the db originals via save_phone etc.
INCLUDE_OOS = bool(os.environ.get("INCLUDE_OOS"))


def _better_offer(new_availability, new_price, cur):
    if cur is None:
        return True
    new_in = new_availability == "in_stock"
    cur_in = cur.get("availability") == "in_stock"
    if new_in != cur_in:
        return new_in
    return new_price < cur["price"]


def build_offers():
    """Fetch + parse into a dedup dict keyed (variant_key, ram, condition). No DB."""
    codes = fetch_codes()
    print(f"{len(codes)} representative codes on the landing page: {codes}")
    if not codes:
        return {}, False
    families = fetch_families(codes)
    print(f"{len(families)} families from the API")

    best = {}
    for fam in families:
        for m in (fam.get("modelList") or []):
            model = _clean_name(m.get("displayName"), fam.get("fmyMarketingName"))
            if not model or not is_phone(model):
                continue
            storage = normalize_storage(_chip(m, "MOBILE MEMORY"))
            if not storage:
                continue
            mm = re.search(r"\((\d+)\s*GB\s*Memory\)", m.get("displayName") or "", re.I)
            ram = (mm.group(1) + "GB") if mm else None
            price = _num(m.get("promotionPrice")) or _num(m.get("price"))
            if not price:
                continue
            in_stock = (m.get("stockStatusText") or "").lower() == "instock"
            if not in_stock and not INCLUDE_OOS:
                continue
            availability = "in_stock" if in_stock else "out_of_stock"
            vkey = make_variant_key(model, storage, ram)
            bkey = (vkey, ram, CONDITION)
            if not _better_offer(availability, price, best.get(bkey)):
                continue
            rc = m.get("reviewCount")
            review_count = int(rc) if rc not in (None, "", "0") else None
            rating = float(m["ratings"]) if (m.get("ratings") and review_count) else None
            pdp = m.get("pdpUrl") or ""
            url = ("https://www.samsung.com" + pdp) if pdp.startswith("/") else (pdp or LANDING)
            name = (f"{model} {ram}/{storage}" if ram else f"{model} {storage}").strip()
            best[bkey] = {
                "model": model, "storage": storage, "ram": ram, "variant_key": vkey,
                "condition": CONDITION, "price": price, "availability": availability,
                "url": url, "image_url": _img(m.get("largeUrl") or m.get("thumbUrl")),
                "rating": rating, "review_count": review_count, "name": name,
                "color": _chip(m, "COLOR"),
            }
    return best, True


def scrape():
    from datetime import datetime, timezone
    from db import (save_phone, save_price, ensure_image, mark_site_oos,
                    mark_unseen_out_of_stock)
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos(SITE)

    best, ok = build_offers()
    if not ok:
        print("No codes found — landing page layout changed?")
        mark_unseen_out_of_stock(SITE, run_started_at, run_complete=False)
        return

    print(f"\nUnique (variant, condition) offers: {len(best)}")
    in_stock_names = {o["name"] for o in best.values() if o["availability"] == "in_stock"}
    saved = 0
    for o in best.values():
        hosted = None
        if o["image_url"]:
            hosted = ensure_image(o["image_url"], f"{SITE}/{o['variant_key']}.jpg".replace("|", "_"))
        pid = save_phone(
            SITE, o["name"], o["url"], hosted or o["image_url"],
            o["model"], o["storage"], o["ram"], o["variant_key"],
            in_stock=(o["name"] in in_stock_names),
        )
        save_price(
            pid, o["price"], availability=o["availability"], condition=CONDITION,
            url=o["url"], rating=o["rating"], review_count=o["review_count"],
            warranty_label=WARRANTY_LABEL,
        )
        saved += 1
        print(f"  saved: {o['name']:48} ₹{o['price']:.0f}  [{o['availability']}]")

    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=bool(best))
    print(f"\nDone. Saved {saved} offers from {SITE}.")


def dry():
    best, ok = build_offers()
    print(f"\n{len(best)} unique (variant, ram, condition) offers:\n")
    for o in sorted(best.values(), key=lambda x: (x["model"], x["storage"], x["ram"] or "")):
        rv = f" · ★{o['rating']}/{o['review_count']}" if o["rating"] else ""
        print(f"  {o['variant_key']:38} {o['name']:46} {o['color'] or '':18} "
              f"₹{o['price']:.0f} [{o['availability']}]{rv}")


if __name__ == "__main__":
    if "--dry" in sys.argv:
        dry()
    else:
        init_sentry(SITE)
        try:
            scrape()
        except Exception as e:
            log_error(e, site=SITE, phase="scrape")
            raise
