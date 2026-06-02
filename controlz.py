"""
ControlZ scraper (Playwright). Reads exactly what shoppers see.

Per product, per CONDITION (Premium renewed / Saver Series / ...), per STORAGE,
reads the visible "Starting From" price. Keeps the lowest price per
(variant_key, condition). Color / battery / issues are ignored.

Robust choices:
 - desktop viewport (nothing collapses)
 - wait_for_selector, not networkidle (site never idles)
 - scope CONDITION options to the Category section; STORAGE options to the
   Storage section; read price from the "Starting From" block (.text-primary)

Run with:  python3 controlz.py
"""
import re
import json
import time
import requests
from playwright.sync_api import sync_playwright
from normalize import clean_model, normalize_storage, normalize_ram, make_variant_key, parse_size_string, normalize_condition, is_phone
from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock

SITE = "controlz"
LISTING_URL = "https://www.controlz.world/store"
BASE_URL = "https://www.controlz.world"
DELAY_SECONDS = 1.5
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def get_product_slugs():
    """Get all product slugs from ControlZ listing page RSC payload.
    Uses the adjacent productType+slug pattern which is reliable.
    Titles are not extracted here — they're read from the product page during scraping.
    """
    resp = requests.get(LISTING_URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    chunks = re.findall(r'self\.__next_f\.push\(\[\d+,(".*?")\]\)', resp.text, re.S)
    payload = "".join(json.loads(c) for c in chunks if c.startswith('"'))

    SKIP_SLUGS = {"power-bank", "powerbank", "charger", "cable", "case", "cover",
                  "earphone", "headphone", "adapter", "hub", "stand"}

    seen, products = set(), []
    for m in re.finditer(r'"productType":"phone","slug":"([^"]+)"', payload):
        slug_val = m.group(1)
        if slug_val in seen:
            continue
        if slug_val in SKIP_SLUGS or any(sk in slug_val for sk in SKIP_SLUGS) or not is_phone("", slug_val):
            continue
        seen.add(slug_val)
        products.append({"slug": slug_val, "title": slug_val})  # title read from product page
    return products


def section_buttons(page, heading_keyword):
    """Return the option buttons under the section whose <h2> contains keyword.
    We find the h2, then its nearest following .variant-options-container."""
    js = """(kw) => {
      const h2s = Array.from(document.querySelectorAll('h2'));
      const h = h2s.find(e => e.textContent.toLowerCase().includes(kw));
      if (!h) return [];
      // walk up to a container that holds both heading and options
      let node = h;
      for (let i=0; i<6 && node; i++) {
        node = node.parentElement;
        if (!node) break;
        const cont = node.querySelector('.variant-options-container');
        if (cont) {
          return Array.from(cont.querySelectorAll('button')).map(b => {
            const s = b.querySelector('span');
            return (s ? s.textContent : b.textContent).trim();
          });
        }
      }
      return [];
    }"""
    return page.evaluate(js, heading_keyword.lower())


def click_option(page, heading_keyword, label):
    """Click the button under the given section whose text matches label."""
    js = """([kw, lbl]) => {
      const h2s = Array.from(document.querySelectorAll('h2'));
      const h = h2s.find(e => e.textContent.toLowerCase().includes(kw));
      if (!h) return false;
      let node = h;
      for (let i=0; i<6 && node; i++) {
        node = node.parentElement;
        if (!node) break;
        const cont = node.querySelector('.variant-options-container');
        if (cont) {
          const btns = Array.from(cont.querySelectorAll('button'));
          const b = btns.find(x => (x.querySelector('span')?.textContent || x.textContent).trim().includes(lbl));
          if (b) { b.click(); return true; }
        }
      }
      return false;
    }"""
    return page.evaluate(js, [heading_keyword.lower(), label])


def read_price(page):
    """Read the 'Starting From' price (the .text-primary <p> near that label)."""
    js = """() => {
      const ps = Array.from(document.querySelectorAll('p'));
      // find a price <p> with class text-primary containing a rupee figure
      const pe = ps.find(p => /text-primary/.test(p.className) && /₹\\s?[\\d,]{3,}/.test(p.textContent));
      return pe ? pe.textContent.trim() : null;
    }"""
    return page.evaluate(js)



def read_image(page):
    """Grab the main product image from the rendered page, unwrapping Next.js
    /_next/image?url=<real> optimizer URLs to the real CDN link."""
    js = """() => {
      const imgs = Array.from(document.querySelectorAll('img'));
      const pick = imgs.find(i => /cloudfront/.test(i.src) && i.naturalWidth > 100)
                || imgs.find(i => i.naturalWidth > 200);
      return pick ? pick.src : null;
    }"""
    raw = page.evaluate(js)
    if raw and "/_next/image" in raw:
        import urllib.parse as up
        q = up.urlparse(raw).query
        u = up.parse_qs(q).get("url", [None])[0]
        if u:
            return up.unquote(u)
    return raw



def read_rating_reviews(page):
    """Read rating (e.g. 4.7) and review count (e.g. 21) from the rendered page.
    The header shows something like '4.7 . 21 REVIEWS'. Returns (rating, count)."""
    js = """() => {
      const txt = document.body.innerText;
      const m = txt.match(/([0-9](?:\\.[0-9])?)\\s*[·.]\\s*([0-9]+)\\s*REVIEWS/i);
      if (m) return {rating: m[1], count: m[2]};
      const m2 = txt.match(/([0-9]+)\\s*REVIEWS/i);
      if (m2) return {rating: null, count: m2[1]};
      return {rating: null, count: null};
    }"""
    r = page.evaluate(js)
    rating = float(r["rating"]) if r and r.get("rating") else None
    count = int(r["count"]) if r and r.get("count") else None
    return rating, count


def parse_price(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return float(digits) if digits else None


def scrape_product(page, slug):
    url = f"{BASE_URL}/products/{slug}"
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    # Read title from h1 before any early return
    page_title = page.evaluate("""() => {
        const h1 = document.querySelector('h1');
        return h1 ? h1.innerText.trim() : null;
    }""")
    try:
        page.wait_for_selector(".variant-options-container", timeout=20000)
    except Exception:
        return url, page_title, None, None, None, []
    page.wait_for_timeout(1200)
    image_url = read_image(page)
    rating, review_count = read_rating_reviews(page)

    conditions = section_buttons(page, "category") or ["Premium renewed"]
    storages = section_buttons(page, "storage") or [None]

    out = []
    for cond in conditions:
        if cond:
            click_option(page, "category", cond)
            page.wait_for_timeout(500)
        for st in storages:
            if st:
                click_option(page, "storage", st)
                page.wait_for_timeout(500)
            # Get all available colors, click each, keep lowest price
            colors = section_buttons(page, "color")
            available_colors = [c for c in colors if c] if colors else []
            if available_colors:
                prices = []
                for color in available_colors:
                    click_option(page, "color", color)
                    page.wait_for_timeout(400)
                    p_val = parse_price(read_price(page))
                    if p_val is not None:
                        prices.append(p_val)
                price = min(prices) if prices else None
            else:
                price = parse_price(read_price(page))
            if price is not None:
                out.append((normalize_condition(cond), st, price))
    return url, page_title, image_url, rating, review_count, out


def scrape():
    from datetime import datetime, timezone
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos("controlz")
    products = get_product_slugs()
    print(f"Found {len(products)} products to visit.")
    best = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
        for idx, prod in enumerate(products, 1):
            slug, parent_title = prod["slug"], prod["title"]
            try:
                url, page_title, image_url, rating, review_count, rows = scrape_product(page, slug)
            except Exception as e:
                print(f"  [{idx}/{len(products)}] {slug}: ERROR {str(e)[:80]}")
                time.sleep(DELAY_SECONDS); continue

            # Filter non-phones by the ACTUAL product title, not just the slug.
            # The slug-level is_phone() check at collection time misses items whose
            # slug looks phone-ish but whose title is an accessory (e.g. a power
            # bank tagged productType "phone" in the listing payload).
            model = clean_model(page_title or parent_title)
            if not model or not is_phone(model, slug):
                print(f"  [{idx}/{len(products)}] {slug}: skipped (not a phone: {page_title!r})")
                time.sleep(DELAY_SECONDS); continue

            for cond, st, price in rows:
                storage = normalize_storage(st) if st else None
                ram = normalize_ram(parent_title)
                vkey = make_variant_key(model, storage, ram)
                key = (vkey, cond)
                cand = {"model": model, "storage": storage, "ram": ram,
                        "variant_key": vkey, "condition": cond, "price": price,
                        "url": url, "image_url": image_url,
                        "rating": rating, "review_count": review_count,
                        "name": f"{model} {storage or ''}".strip()}
                if key not in best or price < best[key]["price"]:
                    best[key] = cand
            print(f"  [{idx}/{len(products)}] {slug}: {len(rows)} price points")
            time.sleep(DELAY_SECONDS)
        browser.close()

    saved = 0
    for (vkey, cond), o in best.items():
        # Self-host the image on first sighting; fall back to source URL on failure.
        hosted = None
        if o["image_url"]:
            ext = ".jpg"
            if ".png" in o["image_url"].lower(): ext = ".png"
            elif ".webp" in o["image_url"].lower(): ext = ".webp"
            dest = f"{SITE}/{o['variant_key']}{ext}".replace("|", "_")
            hosted = ensure_image(o["image_url"], dest)
        final_image = hosted or o["image_url"]

        pid = save_phone(SITE, o["name"], o["url"], final_image,
                         o["model"], o["storage"], o["ram"], o["variant_key"])
        save_price(pid, o["price"], availability="in_stock", condition=o["condition"],
                   rating=o.get("rating"), review_count=o.get("review_count"))
        saved += 1
        print(f"  saved: {o['name']:28} [{cond:16}] ₹{o['price']:.0f}")

    # Phones not seen in this run -> out of stock (guarded against partial runs).
    mark_unseen_out_of_stock(SITE, run_started_at)

    print(f"\nDone. Saved {saved} (variant, condition) offers from {SITE}.")


if __name__ == "__main__":
    scrape()