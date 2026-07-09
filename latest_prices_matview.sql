-- ---------------------------------------------------------------------------
-- latest_prices_mat: materialize the per-(phone, condition) latest price so the
-- `offers` view stops re-deriving it (DISTINCT ON over the full, append-only
-- `prices` history — 200k+ rows and growing) on EVERY web request.
--
-- WHY (incident 2026-07-07): every /phone/[variant] render pages through the
-- whole `offers` view; `offers` joined the PLAIN `latest_prices` view, which
-- scans the entire `prices` table each call (~600ms+ warm, worse cold/loaded).
-- Under load (ISR regen waves, crawlers on uncached long-tail pages, overlap
-- with a scrape writing to `prices`) a single page query crossed the anon role's
-- statement_timeout and PostgREST cancelled it -> the RSC render threw -> Vercel
-- 500s on /phone/[variant]. It worsens monotonically as `prices` grows.
--
-- FIX: precompute latest_prices into a materialized view (a ~14.6k-row snapshot,
-- one row per phone×condition), unique-indexed so it can REFRESH ... CONCURRENTLY.
-- `offers` now joins the matview (few-ms lookup) instead of re-scanning history.
-- The scraper refreshes it at the END of the pipeline (normalize_db), after all
-- price writes and BEFORE notify.py reads offers, so prices are as-fresh-as the
-- scrape (which is the only moment they change anyway).
--
-- NOTE: `offers` still joins the LIVE `phones` table for model/storage/ram/
-- in_stock/url — only the price/condition/rating/warranty columns (which change
-- solely on a scrape) come from the snapshot, so admin edits to phones.ram /
-- images reflect immediately; no staleness regression.
--
-- Idempotent. Apply order on a fresh DB: after `prices`/`latest_prices` exist,
-- run this, THEN specs_schema.sql (whose `offers` now references latest_prices_mat).
-- ---------------------------------------------------------------------------

-- The snapshot mirrors the plain latest_prices view (single source of the
-- "latest per phone+condition" dedup logic, incl. warranty_days/label + url).
create materialized view if not exists latest_prices_mat as
  select * from latest_prices
  with no data;

-- Unique key = the DISTINCT ON key of latest_prices (verified unique, no NULL
-- condition). Required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
create unique index if not exists latest_prices_mat_pk
  on latest_prices_mat (phone_id, condition);

-- First populate (safe to re-run; only does work when the view has no data).
refresh materialized view latest_prices_mat;

-- Callable refresh for the scraper (supabase-py has no raw-SQL path, so it goes
-- through PostgREST rpc). Plain (non-concurrent) REFRESH: it briefly takes an
-- AccessExclusive lock (~0.6-1s) — acceptable at scrape cadence (every 3h) and
-- required because REFRESH ... CONCURRENTLY cannot run inside the transaction
-- PostgREST wraps an rpc() in. (If a lock-free refresh is ever needed, drive
-- `refresh materialized view concurrently latest_prices_mat` from pg_cron, which
-- runs each statement in its own session outside a txn block.)
create or replace function refresh_latest_prices()
  returns void
  language plpgsql
  security definer
  set search_path = public
as $$
begin
  refresh materialized view latest_prices_mat;
end;
$$;

grant execute on function refresh_latest_prices() to service_role;

-- Repoint `offers` at the snapshot. Column list/order/types are IDENTICAL to the
-- current view, so CREATE OR REPLACE is non-destructive (missing_images /
-- ram_collisions, which don't depend on offers, are untouched).
create or replace view offers as
 select coalesce(ph.canonical_key, ph.variant_key) as variant_key,
    ph.model, ph.storage, ph.ram, ph.site, ph.name, ph.url,
    coalesce(sp.image_url, sp.image_fallback, ph.image_url) as image_url,
    lp.price, lp.availability, lp.condition, lp.rating, lp.review_count,
    lp.warranty_days, lp.warranty_label, lp.url as condition_url,
    s.display_name as store_name, s.logo_url, s.default_warranty_days, s.trust_score,
    ph.in_stock, ph.last_seen_at,
    sp.specs                as specs,
    sp.gsm_url              as gsm_url
   from phones ph
     join latest_prices_mat lp on lp.phone_id = ph.id
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
