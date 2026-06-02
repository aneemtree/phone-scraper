## Keeping this file current
ALWAYS update this CLAUDE.md whenever you merge a change to `main`. In the same
work that does the merge, edit the relevant section(s) to reflect what changed
(new/removed scraper, schema change, availability/normalization rule, workflow,
data source, etc.) and commit the doc update alongside (or immediately after)
the merge. This file is the source of truth for how the system works — a merge
that changes behaviour without updating it is incomplete.

## Standard scraping approach (apply to ALL scrapers)

### Price capture rule
For every (condition, storage) combination, always cycle through ALL available
colors and capture the LOWEST price. Never save the first/default color price.
Colors affect price — e.g. green might be ₹1,500 cheaper than black for the
same condition+storage.

### Availability rule
Only scrape what is visibly available to the user on the product page — the
rendered UI (opacity-50, line-through, disabled, no Add-to-Cart) is the source
of truth. Never trust a raw inventory/`qty` number: many stores keep `qty > 0`
on sold-out variants (phantom inventory), and JSON-LD often lists draft items.

Exception (validated only): some stores embed a per-variant availability FLAG in
their server payload that provably matches the rendered UI — use it to avoid a
browser per product. Confirmed cases:
  - Ovantica: each RSC variant has `stock_update` = `in_stock`/`out_of_stock`
    (matches Add-to-Cart; `qty` does NOT). Scraper is requests-only.
  - Cashify: each RSC variant has `availableInventory` (>0 = in stock); the
    buy button is `Buy Now` vs `Notify Me!`. Scraper is requests-only.
Always re-validate such a flag against the rendered button before relying on it.

### Condition handling
Each product page may have multiple condition grades (e.g. Fair/Good/Superb on
Cashify, Premium Renewed/Saver Series on ControlZ). For each available condition,
for each available storage, take the minimum price across available colors, and
save one row per (variant_key, condition) with that lowest price.

Two implementation styles:
  - DOM/click sites (ControlZ): click condition → storage → colors, read price.
  - Payload sites (Cashify, Ovantica): parse the embedded Next.js RSC payload
    (`self.__next_f.push([...])`) for the full variant matrix — grade, storage,
    color, price, availability flag, and the per-variant id — no clicking. This
    is faster and more reliable; prefer it when the payload is complete.

### Normalization (normalize.py)
All scrapers must pass model names through clean_model() and storage through
normalize_storage() before calling make_variant_key(). Never save raw names.
Key noise words already stripped: 5G, 4G, India, With Box, Open Box, Series,
model numbers (SM-G991B), years (2021), Refurbished, Renewed, colors, etc.
Brand casing: iPhone, iPad, OnePlus, POCO, iQOO are normalized.

### variant_key & RAM
make_variant_key() uses model + storage ONLY (RAM excluded) so the same phone
groups across stores, since most stores don't surface RAM. Exception: oldsold
sells the same storage at different RAM (8GB/256GB vs 12GB/256GB) at different
prices, so its scraper keys the dedup dict by (variant_key, ram, condition) and
folds RAM into the saved `name` to keep both as distinct offers. They still share
the storage-only variant_key (so they sit under one cross-store card). Note:
normalize_ai Pass 2 groups by model+storage too (RAM-agnostic).

### Out-of-stock tracking
Availability is tracked on the `phones` table: `in_stock` (bool), `last_seen_at`,
plus `updated_at` (auto-maintained by a trigger; also on prices/stores/specs).
Every scraper:
  - captures `run_started_at` at the start of scrape();
  - save_phone() stamps `last_seen_at=now`, `in_stock=true` on every sighting;
  - calls `mark_unseen_out_of_stock(SITE, run_started_at)` at the end, which
    (a) flips `in_stock=false` for phones not seen this run, and (b) appends an
    `out_of_stock` price snapshot for any (phone, condition) not refreshed this
    run, so latest_prices/offers stop showing a sold-out grade as in stock.
A guard (min_seen_ratio) skips the sweep if a partial/crashed run saw too few
phones, so it can't wipe a store. The offers view exposes `in_stock`/`last_seen_at`.

### Variant deep-links
Save the per-variant URL, not the bare product URL, so "Visit store" lands on the
exact offer: Ovantica/Cashify use the variant id in the path (`…/<slug>/<id>`),
Shopify stores (Refit, oldsold) use `?variant=<id>`.

### Manual merge fallback
If two stores produce different variant_keys for the same physical phone
(normalization didn't catch it), set canonical_key on both phones rows in
Supabase to the same value. The offers view uses coalesce(canonical_key, variant_key).

### Image hosting
Download and host images in Supabase Storage bucket "phone-images" on FIRST
sighting only. Path format: {site}/{variant_key}.jpg. Skip if already exists.
Use ensure_image() from db.py.

### Store metadata
After adding a new store, add a row to the stores table:
  insert into stores (site, display_name, website_url) values ('newsite', 'New Site', 'https://...');
Upload the store logo to Supabase Storage "logos" bucket and update logo_url.

### Scrapers & pipeline
Active scrapers: cashify, controlz, refit, xtracover, ovantica, mobilegoo,
sahivalue, oldsold. ControlZ filters non-phones by the actual product TITLE via
is_phone() (a slug-only check missed accessories like power banks).

Per-site data source / speed:
  - cashify, ovantica: requests-only — parse the product RSC payload; Playwright
    used ONLY for the listing/token (ovantica thread-pools the product fetches).
  - refit, oldsold: Shopify products.json (requests-only).
  - xtracover: one Playwright session to scroll the listing; no product pages.
  - controlz: NO usable product API (client calls are analytics; the RSC
    variant data is server-rendered with incomplete `$`-references — storage is
    missing, units only partially inlined). So it stays DOM-based (the rendered
    opacity/line-through/price is the source of truth) but runs each product in
    its own isolated browser via a ThreadPoolExecutor (WORKERS) for speed.

Workflows (GitHub Actions):
  - scrape.yml — full run, `schedule` only (6 AM & 3 PM IST) + workflow_dispatch.
    It does NOT run on push/merge. Runs all scrapers, then normalize_ai.py.
  - scrape-one.yml — manual single-site chooser (workflow_dispatch) for testing
    one scraper. Does NOT run normalize_ai.
GitHub Actions cron is best-effort and often delayed (can be 1–3h late).

normalize_ai.py (runs AFTER all scrapers, full pipeline only): Pass 0 deletes
non-phones (AI), Pass 1 cleans model names, Pass 2 sets canonical_key for
cross-store duplicates (groups by model+storage, RAM-agnostic).

### Testing discipline
Don't assume site structure — test against real data first. When the sandbox
can't reach a site, hand the user a self-contained `python3 - <<'EOF'` block to
run and paste back. Validate a parser on saved/live payloads before editing a
scraper, and never push a new/changed scraper until its output is verified.