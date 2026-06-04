-- GSMArena specs/image enrichment table, keyed by variant_key (the cross-store
-- card identity: model + storage, NOT grade). Idempotent: safe to run whether or
-- not a `specs` table already exists.

create table if not exists specs (
  variant_key text primary key,
  model       text,
  gsm_id      bigint,
  gsm_url     text,
  gsm_name    text,
  image_url   text,            -- canonical product image (R2-hosted), card primary
  specs       jsonb,           -- full GSMArena data-spec sheet
  match_score real,
  status      text not null default 'ok',  -- 'ok' | 'not_found'
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- If a `specs` table already existed with a different shape, add what's missing.
alter table specs add column if not exists model       text;
alter table specs add column if not exists gsm_id      bigint;
alter table specs add column if not exists gsm_url     text;
alter table specs add column if not exists gsm_name    text;
alter table specs add column if not exists image_url   text;
alter table specs add column if not exists specs       jsonb;
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
-- Make the GSMArena image the PRIMARY card image.
-- The offers view should expose: coalesce(s.image_url, p.image_url) as image_url
-- and join specs on coalesce(p.canonical_key, p.variant_key) = s.variant_key.
-- (View DDL lives in Supabase; wire the coalesce there.)
