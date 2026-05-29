## Standard scraping approach (apply to ALL scrapers)

### Price capture rule
For every (condition, storage) combination, always cycle through ALL available
colors and capture the LOWEST price. Never save the first/default color price.
Colors affect price — e.g. green might be ₹1,500 cheaper than black for the
same condition+storage.

### Availability rule
Only scrape what is visibly available to the user on the product page.
Never use JSON-LD structured data or API responses as the source of truth for
availability — they often contain phantom/draft listings not shown to users.
Use the rendered UI: opacity-50, line-through text, disabled states = unavailable.

### Condition handling
Each product page may have multiple condition grades (e.g. Fair/Good/Superb on
Cashify, Premium Renewed/Saver Series on ControlZ). For each available condition:
  - Click it
  - For each available storage: click it
    - For each available color: click it, read price
    - Keep the minimum price across colors
  - Save one row per (variant_key, condition) with the lowest price found

### Normalization (normalize.py)
All scrapers must pass model names through clean_model() and storage through
normalize_storage() before calling make_variant_key(). Never save raw names.
Key noise words already stripped: 5G, 4G, India, With Box, Open Box, Series,
model numbers (SM-G991B), years (2021), Refurbished, Renewed, colors, etc.
Brand casing: iPhone, iPad, OnePlus, POCO, iQOO are normalized.

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