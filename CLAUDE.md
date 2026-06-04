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
sahivalue, oldsold, thephonehub, easyphones, tetro, grest, cellbuddy, budli,
itradeit, gadgetrebirth, maplestore. ControlZ filters non-phones by the actual
product TITLE via is_phone() (a slug-only check missed accessories like power
banks); thephonehub filters on the CLEAN model, not the slug, because its slugs
embed marketing words (e.g. "50mp-ois-camera") that collide with is_phone().

Per-site data source / speed:
  - cashify, ovantica: requests-only — parse the product RSC payload; Playwright
    used ONLY for the listing/token (ovantica thread-pools the product fetches).
  - refit, oldsold, easyphones: Shopify products.json (requests-only). easyphones
    options are Color/Colour + Storage + Condition/Grade (slot order/spelling
    vary → resolved by name via shopify_option_index); grade qualifier in parens
    is stripped ("Superb (Like-New)" → "Superb") and a bare "Like-New" folds into
    "Superb"; prices are rupees; deep-link ?variant=<id>.
  - tetro: Shopify products.json (/collections/all). iPhone-only, all "Pre-loved"
    (like-new) with NO grade option — variants are Storage × Battery Health ×
    Warranty Info, and each COLOR is a separate product. So one row per (model,
    storage) at the LOWEST available price across battery/warranty/color, condition
    fixed to "Like New" (the store's own label). Storage resolved by name via
    shopify_option_index; products with no Storage option are skipped; prices are
    rupees; deep-link ?variant=<id>.
  - grest: Shopify products.json (/collections/iphones). iPhone-only; titles omit
    "Apple" (clean_model auto-prefixes). Options Storage/Condition/Color (resolved
    by name; one product has a stray " Condition"); grades Fair/Good/Superb (same
    vocab as Cashify). One row per (storage, grade) at the lowest color price.
    The collection products.json caps page size (~30) so it paginates until an
    empty page (not <250); prices are rupees; deep-link ?variant=<id>.
  - cellbuddy: WooCommerce (WordPress under the /buddy/ subpath), requests-only.
    Listing from the Store API (/buddy/wp-json/wc/store/v1/products?category=94 =
    iPhone). NO grade variant axis (variants are only Storage × Color); CellBuddy
    lists each condition as a SEPARATE product, identified by category membership:
    "No Face ID"/"No Touch ID" keep those labels, plain or "Refurbished" → "Unknown
    Condition" — so one model shows several condition rows. The condition suffix is
    stripped from the model name. Storage slug is bare ("128") so storage is read
    from the attribute term NAME via a slug→name map. Per-variant price/stock from
    the embedded data-product_variations (wc-ajax fallback like thephonehub; single-
    storage uses the Store API min price). Prices: variation display_price is rupees,
    Store API prices.price is minor units (÷100). Deep-link via ?attribute_pa_*.
  - budli: Shopify products.json (/collections/mobile-phones), requests-only,
    all-brands. Unlike the other Shopify stores, Budli bakes model + storage +
    colour + CONDITION into the product TITLE and ~90% of products are single-
    variant ("Default Title"), so model/storage/condition are parsed from the
    title. Condition is the trailing parenthetical: "Good Condition" → Good,
    "Refurbished"/none → Unknown Condition, "Unboxed - Brand Warranty" kept as-is,
    "Functional Issue" → product SKIPPED (defective, not listed). Storage from a
    Storage/"Storgae"(typo) variant option when present, else the largest GB/TB
    token in the title (RAM stripped first); one row per (storage, condition) at
    the lowest color price. Prices are rupees; deep-link ?variant=<id>. NOTE: many
    Budli titles leak colour qualifiers (Solar/Sierra/Forest/Awesome <c>/etc.) and
    a leading "Used" — these are stripped in clean_model() (see COLORS additions
    and the pre-owned/used noise pass); "Vivo iQOO …" → "iQOO …" so it shares the
    iQOO brand chip.
  - itradeit: WordPress/WooCommerce (itradeit.in), requests-only, all-brands.
    Two product categories carry the CONDITION (there is no grade/condition
    variant axis — axes are only pa_color × pa_storage, so condition = category
    membership like CellBuddy): open-box-phones (id 438) → "Open Box";
    certified-refurbished (id 60, "Refurbished Phones") → "Unknown Condition".
    Listing from the Store API (/wp-json/wc/store/v1/products?category=<id>);
    per-variant price/stock/image from the product page's embedded
    data-product_variations JSON (matrices are tiny, always inlined — no wc-ajax
    needed; a no-form product falls back to the Store API min price). Storage
    BUNDLES RAM (terms "12GB/256GB") so, like oldsold, RAM is folded into the name
    and the dedup key (variant_key, ram, condition) while make_variant_key stays
    storage-only for cross-store grouping. itradeit DROPS "Galaxy" from Samsung
    titles ("Samsung S25 Ultra") — clean_model re-inserts it (see the Galaxy rule
    in normalize.py) so keys match the other stores. Prices: embedded display_price
    is rupees (display_regular_price is the strike, ignored); Store API prices.price
    is minor units (÷100). Deep-link via ?attribute_pa_storage/_color.
  - gadgetrebirth: custom React storefront (gadgetrebirth.com), requests-only,
    all-brands. The SPA is backed by its OWN JSON API at api.gadgetrebirth.com;
    the catalog endpoint returns the FULL per-product variant matrix inline, so no
    per-product fetch and no browser: GET /api/products?limit=100&skip=<n>.
    Pagination is by `skip` ONLY (page/large-limit params are ignored; a single
    response caps ~200 rows), so walk skip in steps of 100 until a short/empty page.
    The endpoint returns ALL ~1100 products across every category (incl sold-out
    historical) — keep category=="phones". Each product has variants[] with
    options{Condition, Storage, Color}, integer rupee `price`, `compareAtPrice`
    (strike, ignored), `stock`, `active`. One row per (condition, storage) at the
    LOWEST color price. Availability = `active AND stock>0` (VALIDATED against the
    rendered UI — the payload carries both phantom cases this guards: active=false
    & stock>0, and active=true & stock=0, both correctly excluded; raw stock and
    top-level `status` are NOT trusted alone). The API `name` carries no
    storage/colour, so build_model() unwraps parenthesised model ids ("Phone (2)"
    → "Nothing Phone 2", which clean_model would otherwise delete) and prepends the
    brand slug only when absent (so "Galaxy S25 Ultra"/"Xperia 1 V" get their brand
    chip without doubling "OnePlus"/"iPhone"). Conditions New/Like New/Excellent/
    Good/Fair (norm_condition() maps + fixes the payload's casing noise and the
    "ike-new" typo). Image: product main image. Deep-link: /product/<sku>/ (the SPA
    has no per-variant URL param). `python3 gadgetrebirth.py --dry` fetches+prints
    offers with NO DB for validation. OOS-capable (INCLUDE_OOS).
  - maplestore: Shopify (maplestore.in), iPhone-only, pre-owned, requests-only.
    Uses a CUSTOM variant-group app, NOT Shopify variants: every products.json
    entry is ONE physical unit (variant.sku = device serial), all "Default Title".
    Model/storage/colour are baked into the product TITLE ("iPhone 16 Pro Max -
    256GB - Desert Titanium - IW (28-Jun-26) - Pre-owned") but the per-unit GRADE
    is NOT in the title/tags — it's the active condition swatch in the product PAGE
    HTML (div.option_main_container_condition → the .active_value's data_val). So
    list from /collections/all-iphones/products.json, then fetch each product page
    (ThreadPoolExecutor, WORKERS=4 + 429 backoff — the site rate-limits) to read
    the grade. Page grades Almost New/Superb/Good/Fair (data_val "fiar" is the
    store's typo for Fair); "Almost New" is mapped to "Like New" (shared
    cross-store label), Superb/Good/Fair share the Cashify vocab. Title separators are
    inconsistent (" - " vs tight "Pro Max-256GB-…"), so model+storage are anchored
    on the storage token (\d+GB/TB, in every title): storage = that token, model =
    everything before it (clean_model strips the trailing dash + Dual/E-Sim/colour/
    IW noise). One row per (variant_key, grade) at the LOWEST price across the
    colour/warranty units. Price rupees; availability = variant.available;
    deep-link ?variant=<id>. `python3 maplestore.py --dry` validates with NO DB.
    OOS-capable (INCLUDE_OOS).

### Condition vocabulary
Grades from graded stores are Fair/Good/Superb (Cashify/Grest/ThePhoneHub) and
ControlZ's Premium Renewed/Saver Series; Tetro is "Like New". The vague default
label "Refurbished" is remapped to "Unknown Condition" everywhere via
normalize_condition(), since it's just the ungraded-stock placeholder and isn't
comparable across stores. cellbuddy adds "No Face ID"/"No Touch ID" (store-specific);
budli adds "Unboxed - Brand Warranty" (store-specific) and uses Good for "Good Condition";
itradeit adds "Open Box" (its open-box-phones category; its certified-refurbished
category folds to "Unknown Condition"). gadgetrebirth adds "Like New" (shares
Tetro's label), "Excellent", and "New" (store-specific grades); its "Good"/"Fair"
share the Cashify vocab. maplestore maps its page grade "Almost New" to "Like New"
(shared with gadgetrebirth/Tetro) and uses Superb/Good/Fair from the Cashify vocab.
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
  - scrape-catalog.yml — MONTHLY (1st, 01:00 UTC) + dispatch. Runs the 15 JSON/RSC
    scrapers with INCLUDE_OOS=1 then normalize_db, purely for SEO.
GitHub Actions cron is best-effort and often delayed (can be 1–3h late).

### Out-of-stock catalog (SEO, monthly)
When the `INCLUDE_OOS=1` env var is set (only scrape-catalog.yml sets it), the 15
JSON/RSC scrapers (cashify, ovantica, refit, oldsold, mobilegoo, sahivalue,
thephonehub, easyphones, tetro, grest, cellbuddy, budli, itradeit, gadgetrebirth, maplestore) ALSO save out-of-stock variants: `phones.in_stock=false` + an `out_of_stock` price
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

Separately, every DB call in db.py goes through `_exec(lambda: …)`, which retries
transient connection drops (httpx.RemoteProtocolError "ConnectionTerminated" /
GOAWAY, connect/read timeouts) on a freshly rebuilt client with backoff. Supabase
will close a connection mid-request even well under the 20k-stream cap (idle /
load-balancer recycle); without the retry a single drop crashed a scraper — and
because the GitHub steps ran in sequence, the first crash skipped every later
store. Each scraper step in scrape.yml/scrape-catalog.yml now also carries
`if: ${{ !cancelled() }}` so one store's failure no longer skips the rest or the
normalize pass (the job still reports failure for visibility).

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