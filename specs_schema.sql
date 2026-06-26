-- GSMArena specs/image enrichment table, keyed by variant_key (the cross-store
-- card identity: model + storage, NOT grade). Idempotent: safe to run whether or
-- not a `specs` table already exists.

create table if not exists specs (
  variant_key text primary key,
  model       text,
  gsm_id      bigint,
  gsm_url     text,
  gsm_name    text,
  image_url      text,         -- PRIMARY card image (Beebom / admin), R2-hosted
  image_fallback text,         -- GSMArena image, used only when image_url is null
  image_source   text,         -- 'beebom' | 'admin' | null (source of image_url)
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
alter table specs add column if not exists image_url      text;
alter table specs add column if not exists image_fallback text;
alter table specs add column if not exists image_source   text;
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
-- NOTE: offers reads lp.warranty_days + lp.warranty_label from latest_prices
-- (warranty is stored in DAYS; see the add_warranty_label / warranty_days_canonical
-- migrations). latest_prices is maintained in-DB, not here; it must expose
-- warranty_days + warranty_label for this view to build.
-- offers view: specs and the canonical image are per-MODEL, so every storage
-- variant of a phone shares one spec sheet/image. The lateral picks the best
-- specs row for the model (prefer one with specs, then with an image).
drop view if exists missing_images;
drop view if exists offers;

create view offers as
 select coalesce(ph.canonical_key, ph.variant_key) as variant_key,
    ph.model, ph.storage, ph.ram, ph.site, ph.name, ph.url,
    coalesce(sp.image_url, sp.image_fallback, ph.image_url) as image_url,   -- Beebom primary, GSMArena fallback, store image last resort
    lp.price, lp.availability, lp.condition, lp.rating, lp.review_count,
    lp.warranty_days, lp.warranty_label, lp.url as condition_url,
    s.display_name as store_name, s.logo_url, s.default_warranty_days, s.trust_score,
    ph.in_stock, ph.last_seen_at,
    sp.specs                as specs,
    sp.gsm_url              as gsm_url
   from phones ph
     join latest_prices lp on lp.phone_id = ph.id
     left join stores s on s.site = ph.site
     left join lateral (
       select image_url, image_fallback, specs, gsm_url
         from specs sx
        where lower(sx.model) = lower(ph.model)
        order by (sx.specs is not null) desc,
                 (coalesce(sx.image_url, sx.image_fallback) is not null) desc,
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
                     where lower(sx.model) = lower(ph.model)
                       and coalesce(sx.image_url, sx.image_fallback) is not null)
  group by ph.model;

-- Admin RAM-assign queue: non-Apple phone rows (in-stock AND out-of-stock) with
-- no RAM whose storage (variant_key) ships in >=2 distinct RAMs, so the offer
-- can't be auto-placed on the right per-RAM card. The web folds it into every
-- per-RAM card until an admin assigns the real RAM at /admin/ram (sets
-- phones.ram, which db.save_phone then preserves across scrapes). OOS rows are
-- INCLUDED so missing listings can still be captured; the `in_stock` column is
-- exposed so admin can see status. Apple excluded (iPhones don't vary RAM).
drop view if exists ram_collisions;
create view ram_collisions as
 with rams as (
   select variant_key,
          array_agg(distinct lower(replace(replace(ram, ' ', ''), 'ram', '')))
            filter (where ram is not null) as ram_options
     from phones
    where lower(model) not like 'apple%'
      and lower(model) not like '%iphone%'
      and lower(model) not like '%ipad%'
    group by variant_key
 )
 select p.id, p.site, p.name, p.model, p.storage, p.variant_key, p.url, p.in_stock,
        r.ram_options,
        (select min(pr.price) from prices pr where pr.phone_id = p.id) as last_price
   from phones p
   join rams r on r.variant_key = p.variant_key
  where p.ram is null
    and array_length(r.ram_options, 1) >= 2
    and lower(p.model) not like 'apple%'
    and lower(p.model) not like '%iphone%'
    and lower(p.model) not like '%ipad%'
    -- skip rows the same store already resolved on a sibling row (the RAM for
    -- that store+variant is already known, so this null row is a stale/redundant
    -- duplicate). normalize_db Pass 2 deletes such stale OOS orphans entirely.
    and not exists (
      select 1 from phones q
      where q.site = p.site and q.variant_key = p.variant_key and q.ram is not null
    );

-- ---------------------------------------------------------------------------
-- Manual matching overrides. When a model's normalized name doesn't match
-- GSMArena/Beebom, add a known-good variation here; both matchers try the model
-- name AND these aliases. Keyed by the exact `model` string (case-insensitive).
create table if not exists model_aliases (
  model       text primary key,
  alt_name_1  text,
  alt_name_2  text,
  updated_at  timestamptz not null default now()
);
