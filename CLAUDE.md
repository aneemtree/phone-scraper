## Merging to main
NEVER merge to `main` autonomously. Develop, commit, and push freely on the
feature branch, but only merge to `main` when the user EXPLICITLY asks for it.
Verifying a change against real data is not permission to merge — wait for the
user to say so.

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
A trailing "+" is converted to the word "Plus" (Realme 12+ → Realme 12 Plus) so
make_variant_key (which strips non-alphanumerics) keeps it distinct from the
non-plus model instead of collapsing both to the same key.

### variant_key & RAM
make_variant_key() uses model + storage ONLY (RAM excluded) so the same phone
groups across stores, since most stores don't surface RAM. Exception: oldsold
sells the same storage at different RAM (8GB/256GB vs 12GB/256GB) at different
prices, so its scraper keys the dedup dict by (variant_key, ram, condition) and
folds RAM into the saved `name` to keep both as distinct offers. They still share
the storage-only variant_key (so they sit under one cross-store card). Because
the key is deterministic and storage-only, the same physical phone already shares
one variant_key across stores once names are clean — so cross-store grouping needs
no separate merge step.

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

### Image hosting (Cloudflare R2)
Host images on Cloudflare R2 (zero egress — Supabase's cached egress was the quota
we blew; DB/storage size are tiny). ensure_image() in db.py uploads to R2 on FIRST
sighting only (path `{site}/{variant_key}.jpg`, head_object skip) and returns the
R2 public URL. Config via env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
R2_BUCKET, R2_PUBLIC_BASE_URL (passed at job level in all workflows). If unset,
ensure_image falls back to the store's source image URL, so local/unconfigured runs
still work. The DB stays on Supabase. One-off backfill: migrate_images_to_r2.py.

### Store metadata
After adding a new store, add a row to the stores table:
  insert into stores (site, display_name, website_url) values ('newsite', 'New Site', 'https://...');
Upload the store logo to Supabase Storage "logos" bucket and update logo_url.

### Scrapers & pipeline
Active scrapers: cashify, controlz, refit, xtracover, ovantica, mobilegoo,
sahivalue, oldsold, thephonehub. ControlZ filters non-phones by the actual
product TITLE via is_phone() (a slug-only check missed accessories like power
banks); thephonehub filters on the CLEAN model, not the slug, because its slugs
embed marketing words (e.g. "50mp-ois-camera") that collide with is_phone().

Per-site data source / speed:
  - cashify, ovantica: requests-only — parse the product RSC payload; Playwright
    used ONLY for the listing/token (ovantica thread-pools the product fetches).
  - refit, oldsold: Shopify products.json (requests-only).
  - thephonehub: WooCommerce, requests-only. Listing + metadata from the public
    Store API (/wp-json/wc/store/v1/products?category=160). Per-variant
    price/stock/grade from the product page's embedded `data-product_variations`
    JSON; above WooCommerce's ajax threshold that attribute is the string "False"
    so we enumerate storage×grade×color via `?wc-ajax=get_variation`; a few
    single-variant products embed no form and fall back to the Store API min
    price + storage parsed from the title. Grades (Fair/Good/Superb, same vocab
    as Cashify) exist on SOME products only — one row per (storage, grade), else
    "Refurbished". Availability = is_purchasable + the stock badge + per-variation
    flags; the top-level is_in_stock is phantom (always true) and NOT trusted.
    Prices: variation display_price is rupees; Store API prices.price is minor
    units (÷100). Deep-link via ?attribute_pa_storage/_grade/_color.
  - xtracover: one Playwright session to scroll the listing; no product pages.
  - controlz: NO usable product API (client calls are analytics; the RSC
    variant data is server-rendered with incomplete `$`-references — storage is
    missing, units only partially inlined). So it stays DOM-based (the rendered
    opacity/line-through/price is the source of truth) but runs each product in
    its own isolated browser via a ThreadPoolExecutor (WORKERS) for speed.

Workflows (GitHub Actions):
  - scrape.yml — full run, `schedule` only (6 AM & 3 PM IST) + workflow_dispatch.
    It does NOT run on push/merge. Runs all scrapers, then normalize_db.py.
  - scrape-one.yml — manual single-site chooser (workflow_dispatch) for testing
    one scraper. Does NOT run normalize_db.
  - scrape-catalog.yml — MONTHLY (1st, 01:00 UTC) + dispatch. Runs the 7 JSON/RSC
    scrapers with INCLUDE_OOS=1 then normalize_db, purely for SEO.
GitHub Actions cron is best-effort and often delayed (can be 1–3h late).

### Out-of-stock catalog (SEO, monthly)
When the `INCLUDE_OOS=1` env var is set (only scrape-catalog.yml sets it), the 7
JSON/RSC scrapers (cashify, ovantica, refit, oldsold, mobilegoo, sahivalue,
thephonehub) ALSO save out-of-stock variants: `phones.in_stock=false` + an `out_of_stock` price
snapshot at the LOWEST selling price (not the strike price), so model pages exist
for SEO even when nothing is buyable. Default runs are available-only (flag off).
Shared helpers in db.py: `INCLUDE_OOS` and `better_offer(availability, price, cur)`
(in_stock beats out_of_stock; else lower price). Per scraper, phone-level in_stock
is set true iff any of that phone's (site+name) offers is in stock; it self-heals
when a regular run later finds it available. ControlZ (DOM) and Xtracover are NOT
wired for OOS yet — no cheap sold-out source.

At catalog scale the per-row DB writes are huge, so db.py cycles the Supabase
client onto a fresh connection every ~6000 write ops (`_note_op`) — Supabase's
HTTP/2 server sends GOAWAY after ~20k streams on one connection, which otherwise
crashes a big OOS run mid-way. mark_site_oos also deletes in batches (one query
per 100 phones) rather than one-per-phone.

Non-phones: the scraper-level is_phone() only blocks NEW inserts; accessories
already saved before a filter existed persist (mark_unseen flips them OOS, they
don't get deleted), and become visible once the UI shows OOS. normalize_db.py
Pass 0 deletes them deterministically (same is_phone keyword check) — keep the
NON_PHONE_KEYWORDS list in normalize.py current and add new accessory words as
they surface (no SQL purge needed for known keywords).

normalize_db.py (runs AFTER all scrapers, full pipeline + monthly catalog):
deterministic, no AI/API key. Pass 0 deletes non-phones via is_phone(); Pass 1
re-runs clean_model()/make_variant_key() over every row so existing data picks up
normalization-rule improvements in place (recomputes model + variant_key; leaves
the raw `name`). Cross-store duplicate merging is NOT a separate step — the
deterministic storage-only variant_key already groups the same phone across stores
once names are clean. canonical_key stays for the manual merge fallback only.
This replaced the old AI-based normalize_ai.py (dropped): "learn from what we have
and keep adding cases" — when a bad name/leak shows up, add the rule to
clean_model() (e.g. a color to COLORS, a noise word) and the next run self-heals.

### Error logging (Sentry)
Optional Sentry error logging lives in obs.py: `init_sentry(SITE)` +
`log_error(exc, **tags)`. It's a NO-OP unless the `SENTRY_DSN` env var is set, so
local/sandbox runs are unaffected. Each scraper's `__main__` calls init_sentry
and reports any crash; the per-item `except` loops (cashify/controlz/ovantica/
sahivalue) also call log_error so swallowed product errors are still captured.
The workflows pass `SENTRY_DSN: ${{ secrets.SENTRY_DSN }}` at job level (add the
secret in repo settings to turn it on). sentry-sdk is in requirements.txt.

### Testing discipline
Don't assume site structure — test against real data first. When the sandbox
can't reach a site, hand the user a self-contained `python3 - <<'EOF'` block to
run and paste back. Validate a parser on saved/live payloads before editing a
scraper, and never push a new/changed scraper until its output is verified.