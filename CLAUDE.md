# phone-scraper — Project Context

Python scrapers for RefurbCompare. Auto-runs daily via GitHub Actions.

## Stack
- Python 3.14 in venv
- requests + BeautifulSoup for plain HTML sites
- Playwright (Chromium) for JS-rendered sites
- supabase-py for DB writes
- python-dotenv for local secrets

## Secrets
- Stored in .env locally (never commit)
- On GitHub: repo secrets SUPABASE_URL and SUPABASE_SERVICE_KEY
- load_dotenv() at top of db.py handles both cases

## Key files
- controlz.py — Playwright scraper. Visits each product page, reads visible Category options and prices per storage. Handles Premium Renewed and Saver Series.
- db.py — Supabase helpers: save_phone(), save_price(), ensure_image()
- normalize.py — clean_model(), normalize_storage(), normalize_ram(), make_variant_key()
- .github/workflows/scrape.yml — daily 07:30 IST, also triggers on push

## Data model rules
- variant_key format: model-slug_storage e.g. apple-iphone-11_128gb (underscores, no pipes)
- One phones row per (site, variant name). Same variant_key can appear for multiple sites.
- prices is append-only history. One row per (phone_id, condition) per scrape.
- Conditions: ONLY what's visible on the product page UI. Never from JSON-LD (unreliable).
- Availability: use inventory > 0 (status field DRAFT/ACTIVE is unreliable on ControlZ)
- Images: download once on first sighting → upload to Supabase Storage "phone-images" bucket → store our URL. Skip if already exists. Path format: {site}/{variant_key}.jpg

## normalize.py rules
- Colors stripped from model names (long list including "phantom black", "natural titanium" etc)
- "Saver series", "special series", "esim", "5g", "titanium" also stripped
- "iphone" normalized to "iPhone", "ipad" to "iPad"
- Storage normalized: "128-GB" / "128 GB" → "128GB"
- RAM only extracted when explicitly labelled (e.g. "8GB RAM") — never from storage figures

## ControlZ scraper notes
- Listing page uses self.__next_f RSC payload chunks (not __NEXT_DATA__)
- Product pages rendered by Playwright at 1366x900 viewport (desktop so nothing collapses)
- wait_until="domcontentloaded" + wait_for_selector — NOT networkidle (site never idles)
- Category options: visible buttons only (hidden ones are storage/color selectors)
- Price read from <p class="...text-primary"> near "Starting From" label
- Rating/reviews read from page text matching pattern "4.7 · 21 REVIEWS"
- 1.5s delay between product pages to be polite

## Stores to build next
- Cashify (https://www.cashify.in/buy-refurbished-mobile-phones/all-phones)
  Plain HTML (price found in View Source). Multiple condition grades expected.
- Croma (https://www.croma.com/phones-wearables/mobile-phones/refurbished-mobile-phones/c/191)
  Plain HTML.
- Refit — URL not yet confirmed. Plain HTML.
- Xtracover (https://www.xtracover.com/buy-refurbished/mobiles?...)
  JS-rendered (price NOT in View Source) — needs Playwright or API hunting.

## GitHub Actions workflow
- File: .github/workflows/scrape.yml
- Installs Playwright with: python -m playwright install --with-deps chromium
- Secrets injected as env vars (SUPABASE_URL, SUPABASE_SERVICE_KEY)
- To add a new scraper: add "python newsite.py" as a new step in the workflow
