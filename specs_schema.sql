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
create index if not exists specs_model_lower_idx on specs (lower(model));

-- ---------------------------------------------------------------------------
-- offers view: specs and the canonical image are per-MODEL, so every storage
-- variant of a phone shares one spec sheet/image. The lateral picks the best
-- specs row for the model (prefer one with specs, then with an image).
drop view if exists missing_images;
drop view if exists offers;

create view offers as
 select coalesce(ph.canonical_key, ph.variant_key) as variant_key,
    ph.model, ph.storage, ph.ram, ph.site, ph.name, ph.url,
    sp.image_url            as image_url,
    lp.price, lp.availability, lp.condition, lp.rating, lp.review_count,
    lp.warranty_months, lp.url as condition_url,
    s.display_name as store_name, s.logo_url, s.default_warranty_months, s.trust_score,
    ph.in_stock, ph.last_seen_at,
    sp.specs                as specs,
    sp.gsm_url              as gsm_url
   from phones ph
     join latest_prices lp on lp.phone_id = ph.id
     left join stores s on s.site = ph.site
     left join lateral (
       select image_url, specs, gsm_url
         from specs sx
        where lower(sx.model) = lower(ph.model)
        order by (sx.specs is not null) desc, (sx.image_url is not null) desc,
                 sx.updated_at desc
        limit 1
     ) sp on true
  where coalesce(ph.canonical_key, ph.variant_key) is not null;

-- Admin gap list: in-stock MODELS with no canonical image yet.
create view missing_images as
 select ph.model,
        max(ph.name)  as sample_name,
        count(*)      as offer_count
   from phones ph
  where ph.in_stock = true
    and not exists (select 1 from specs sx
                     where lower(sx.model) = lower(ph.model) and sx.image_url is not null)
  group by ph.model;
