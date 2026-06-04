-- GSMArena specs/image enrichment table, keyed by variant_key (the cross-store
-- card identity: model + storage, NOT grade). Idempotent: safe to run whether or
-- not a `specs` table already exists.

create table if not exists specs (
  variant_key text primary key,
  model       text,
  gsm_id      bigint,
  gsm_url     text,
  gsm_name    text,
  image_url    text,           -- canonical product image (R2-hosted), card primary
  image_source text,           -- 'gsmarena' | 'admin' | null
  specs        jsonb,          -- full GSMArena data-spec sheet
  match_score  real,
  status       text not null default 'ok',  -- 'ok' | 'not_found'
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- If a `specs` table already existed with a different shape, add what's missing.
alter table specs add column if not exists model       text;
alter table specs add column if not exists gsm_id      bigint;
alter table specs add column if not exists gsm_url     text;
alter table specs add column if not exists gsm_name    text;
alter table specs add column if not exists image_url    text;
alter table specs add column if not exists image_source text;
alter table specs add column if not exists specs        jsonb;
alter table specs add column if not exists match_score real;
alter table specs add column if not exists status      text not null default 'ok';
alter table specs add column if not exists created_at  timestamptz not null default now();
alter table specs add column if not exists updated_at  timestamptz not null default now();

-- updated_at auto-maintenance (same pattern as phones/prices/stores).
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists specs_set_updated_at on specs;
create trigger specs_set_updated_at
  before update on specs
  for each row execute function set_updated_at();

-- Fast lookup of which keys still need enrichment.
create index if not exists specs_status_idx on specs (status);

-- ---------------------------------------------------------------------------
-- offers view: the canonical GSMArena/admin image is THE card image (stores are no
-- longer scraped for images). Run AFTER populating specs (python3 gsmarena.py) so
-- cards aren't blank during the switch.
create or replace view offers as
 select coalesce(ph.canonical_key, ph.variant_key) as variant_key,
    ph.model, ph.storage, ph.ram, ph.site, ph.name, ph.url,
    sp.image_url            as image_url,     -- canonical (GSMArena/admin) image
    lp.price, lp.availability, lp.condition, lp.rating, lp.review_count,
    lp.warranty_months, lp.url as condition_url,
    s.display_name as store_name, s.logo_url, s.default_warranty_months, s.trust_score,
    ph.in_stock, ph.last_seen_at,
    sp.specs                as specs,
    sp.gsm_url              as gsm_url
   from phones ph
     join latest_prices lp on lp.phone_id = ph.id
     left join stores s on s.site = ph.site
     left join specs sp on sp.variant_key = coalesce(ph.canonical_key, ph.variant_key)
  where coalesce(ph.canonical_key, ph.variant_key) is not null;

-- Admin gap list: in-stock phones with no canonical image yet (no GSMArena match,
-- or matched without an image, or awaiting an admin upload). The admin reads this,
-- uploads an image, and writes specs.image_url (image_source='admin') — e.g. via
-- `python3 gsmarena.py --set-image <variant_key> <image_url>`.
create or replace view missing_images as
 select coalesce(ph.canonical_key, ph.variant_key) as variant_key,
        max(ph.model) as model,
        max(ph.name)  as sample_name,
        count(*)      as offer_count
   from phones ph
     left join specs sp on sp.variant_key = coalesce(ph.canonical_key, ph.variant_key)
  where ph.in_stock = true
    and sp.image_url is null
  group by 1;
