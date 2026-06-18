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
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright
from normalize import clean_model, normalize_storage, normalize_ram, make_variant_key, parse_size_string, normalize_condition, is_phone
# db / obs are imported lazily inside scrape()/__main__ so the pure-DOM helpers
# (scrape_product etc.) can be imported + validated WITHOUT the DB stack
# (httpx/supabase). A local DOM check: `from controlz import scrape_product`.

SITE = "controlz"
LISTING_URL = "https://www.controlz.world/store"
BASE_URL = "https://www.controlz.world"
DELAY_SECONDS = 1.5
WORKERS = 5  # concurrent product browsers
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
      // A button is AVAILABLE only if it isn't disabled/greyed/struck-through.
      // ControlZ greys out the condition/storage/color options it doesn't sell
      // for the current selection (opacity-*, line-through, cursor-not-allowed,
      // aria-disabled). The rendered availability is the source of truth, so we
      // never return an unavailable option — that's what produced phantom
      // (condition, storage) combos before.
      const avail = (b) => !b.disabled
        && b.getAttribute('aria-disabled') !== 'true'
        && !/opacity-[2-6]0|line-through|cursor-not-allowed|disabled|unavailable|sold-?out/i
             .test((b.className||'') + ' ' + (b.querySelector('span')?.className||''));
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
          return Array.from(cont.querySelectorAll('button')).filter(avail).map(b => {
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
      const avail = (b) => !b.disabled
        && b.getAttribute('aria-disabled') !== 'true'
        && !/opacity-[2-6]0|line-through|cursor-not-allowed|disabled|unavailable|sold-?out/i
             .test((b.className||'') + ' ' + (b.querySelector('span')?.className||''));
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
          // Click only if the matched option is actually available; if it exists
          // but is greyed/disabled, report false so the caller skips this combo
          // (this is what kills the phantom condition×storage rows).
          if (b && avail(b)) { b.click(); return true; }
          if (b) return false;
        }
      }
      return false;
    }"""
    return page.evaluate(js, [heading_keyword.lower(), label])


def active_option(page, heading_keyword):
    """Return the label of the SELECTED button in a section, or None.

    ControlZ marks the SELECTED option with the Tailwind class `outline-primary`
    (the accent outline); unselected options carry `outline-textSecondary` (grey).
    That class is the only reliable selected-state signal. Needed because picking
    a storage that doesn't belong to the current category silently FLIPS the
    active category (256GB is Saver-only, so choosing it under "Premium renewed"
    switches the selection to "Saver Series")."""
    js = """(kw) => {
      const isSel = (b) => /\\boutline-primary\\b/.test(b.className || '');
      const h2s = Array.from(document.querySelectorAll('h2'));
      const h = h2s.find(e => e.textContent.toLowerCase().includes(kw));
      if (!h) return null;
      let node = h;
      for (let i=0; i<6 && node; i++) {
        node = node.parentElement;
        if (!node) break;
        const cont = node.querySelector('.variant-options-container');
        if (cont) {
          const b = Array.from(cont.querySelectorAll('button')).find(isSel);
          if (!b) return null;
          const s = b.querySelector('span');
          return (s ? s.textContent : b.textContent).trim();
        }
      }
      return null;
    }"""
    return page.evaluate(js, heading_keyword.lower())


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
    read_title = lambda: page.evaluate(
        "() => { const h1 = document.querySelector('h1'); return h1 ? h1.innerText.trim() : null; }")
    try:
        page.wait_for_selector(".variant-options-container", timeout=20000)
    except Exception:
        # Read the title even on the no-options early-return (it loads with the page).
        return url, read_title(), None, None, None, []
    page.wait_for_timeout(1200)
    # Read the h1 AFTER content settles — reading it right after domcontentloaded
    # returned None (the React title hadn't rendered yet). The h1 carries a
    # marketing suffix ("Apple iPhone 13 - Certified Refurbished | ControlZ") —
    # keep only the model name (everything before the first " - " or " | ").
    page_title = read_title()
    if page_title:
        page_title = re.split(r"\s+[-|]\s+", page_title)[0].strip()
    image_url = read_image(page)
    rating, review_count = read_rating_reviews(page)

    # Category buttons that aren't selected carry a price DELTA ("Saver Series –
    # ₹7310"); strip it so the label is just the condition name.
    def clean_cond(s):
        return re.sub(r"\s*[–-]?\s*₹[\d,]+.*$", "", s or "").strip()

    conditions = [clean_cond(c) for c in (section_buttons(page, "category") or ["Premium renewed"])]
    storages = section_buttons(page, "storage") or [None]

    out, seen = [], set()
    for cond in conditions:
        for st in storages:
            # Re-select the condition for EVERY storage: picking a storage that
            # belongs to a different category flips the active category, so the
            # selection from the previous iteration is stale.
            if cond:
                click_option(page, "category", cond)
                page.wait_for_timeout(400)
            if st:
                if not click_option(page, "storage", st):
                    continue
                page.wait_for_timeout(500)
            # PHANTOM-COMBO GUARD: ControlZ shows storages that don't belong to
            # the selected category, and choosing one silently FLIPS the active
            # category (e.g. 256GB is Saver-only, so picking it under "Premium
            # renewed" switches the selection to "Saver Series" — and the price
            # then shown is the Saver price). So after selecting, read which
            # category is ACTUALLY selected (the blue-bordered button); if it no
            # longer matches the intended one, this (condition, storage) combo
            # doesn't exist — skip it. This kills the mislabeled phantom rows.
            if cond:
                active = clean_cond(active_option(page, "category"))
                if active and normalize_condition(active) != normalize_condition(cond):
                    continue
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
            key = (normalize_condition(cond), st)
            if price is not None and key not in seen:
                seen.add(key)
                out.append((normalize_condition(cond), st, price))
    return url, page_title, image_url, rating, review_count, out


def scrape_one(slug):
    """Open an isolated headless browser for ONE product and scrape it. Each
    worker gets its own Playwright instance + browser so products can run
    concurrently (sync Playwright is not shareable across threads). Returns the
    scrape_product() tuple (url, title, image, rating, reviews, rows)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
        try:
            return scrape_product(page, slug)
        finally:
            browser.close()


def scrape():
    from datetime import datetime, timezone
    from db import save_phone, save_price, ensure_image, mark_site_oos, mark_unseen_out_of_stock
    from obs import log_error
    run_started_at = datetime.now(timezone.utc).isoformat()
    mark_site_oos("controlz")
    products = get_product_slugs()
    print(f"Found {len(products)} products to visit ({WORKERS} workers).")
    best = {}
    read_ok = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(scrape_one, prod["slug"]): prod for prod in products}
        for fut in as_completed(futures):
            prod = futures[fut]
            slug, parent_title = prod["slug"], prod["title"]
            try:
                url, page_title, image_url, rating, review_count, rows = fut.result()
            except Exception as e:
                print(f"  {slug}: ERROR {str(e)[:80]}")
                log_error(e, site=SITE, slug=slug)
                continue
            read_ok += 1            # page scraped without error (rows may be empty)

            # Filter non-phones by the ACTUAL product title, not just the slug.
            # The slug-level is_phone() check at collection time misses items whose
            # slug looks phone-ish but whose title is an accessory (e.g. a power
            # bank tagged productType "phone" in the listing payload).
            model = clean_model(page_title or parent_title)
            if not model or not is_phone(model, slug):
                print(f"  {slug}: skipped (not a phone: {page_title!r})")
                continue

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
            print(f"  {slug}: {len(rows)} price points")

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

    # Gate the OOS sweep on scraper HEALTH: fraction of product pages scraped
    # without error (a block/Playwright failure collapses read_ok -> skip sweep).
    ratio = (read_ok / len(products)) if products else 0.0
    run_complete = bool(products) and ratio >= 0.7
    print(f"Read OK: {read_ok}/{len(products)} ({ratio*100:.0f}%) — run_complete={run_complete}")
    mark_unseen_out_of_stock(SITE, run_started_at, run_complete=run_complete)

    print(f"\nDone. Saved {saved} (variant, condition) offers from {SITE}.")


if __name__ == "__main__":
    from obs import init_sentry, log_error
    init_sentry(SITE)
    try:
        scrape()
    except Exception as e:
        log_error(e, site=SITE, phase="scrape")
        raise