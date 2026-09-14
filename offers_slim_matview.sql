-- ---------------------------------------------------------------------------
-- offers_slim_mat + specs_by_model_mat: materialize the web's per-render catalog
-- RPCs so they stop re-aggregating the whole `offers` view on every request.
--
-- WHY (incident 2026-09-13): all_offers_json() reads the `offers` view, whose
-- per-row `specs` LEFT JOIN LATERAL made each call 10-106s; specs_by_model_json()
-- ran an 89s distinct-on over `specs`. Concurrent ISR-regen/crawler calls (the
-- long-tail /[...filter] combos + stale phone pages each fire these) piled up,
-- saturated Postgres into a cascading `canceling statement due to statement
-- timeout` storm (200-480/hr for ~17h) with periodic crash-restarts, timing out
-- the website (/phone/[variant], /[...filter] "upstream request timeout") AND the
-- scrapers (ReadTimeout / pool-timeout / schema-cache APIErrors). Diagnosed from
-- the Postgres slow-query log (all_offers_json at 12s/25s/106s, specs at 89s).
--
-- FIX (same pattern as latest_prices_mat / variant_price_extremes_mat): snapshot
-- both sets into matviews the RPCs read; per-call cost drops to ~0.6s / ~0.25s.
-- Refreshed by the scraper's EXISTING end-of-pipeline refresh_latest_prices()
-- (extended below) — NO scraper/workflow change needed.
--
-- Idempotent. Apply order on a fresh DB: after latest_prices_matview.sql +
-- variant_price_extremes_matview.sql + web_read_functions.sql + specs_schema.sql
-- (offers_slim_mat is built FROM the `offers` view). This file is the FINAL
-- authority for all_offers_json(), specs_by_model_json() and refresh_latest_prices()
-- — if you re-apply any of those earlier files afterwards, re-apply THIS one too.
--
-- TRADEOFF: offers_slim_mat snapshots the FULL offers row, so the `phones` fields
-- (model/storage/ram/in_stock/url) + the resolved image_url become as-of-last-
-- refresh for the LISTING feed instead of live. in_stock/prices change only on a
-- scrape anyway (refresh runs at pipeline end, after mark_unseen); admin edits
-- (/admin/ram, image uploads) now lag until the next scrape's refresh. Acceptable
-- (the web is 6h-ISR-cached) and the price of ending the outage. The live `offers`
-- view is UNCHANGED — notify.py and any direct reader still get live data.
-- ---------------------------------------------------------------------------

-- Functional index so the offers-view specs lateral (where lower(sx.model) =
-- lower(ph.model)) and the specs distinct-on use an index instead of a per-row
-- seq scan of specs (the plain specs_model_idx is on model, not lower(model)).
-- Speeds the matview builds/refreshes AND the live offers view.
create index if not exists specs_lower_model_idx on specs (lower(model));

-- 1. offers snapshot = the offers view WITHOUT the specs/gsm_url blob columns
--    (exactly what all_offers_json emits). to_jsonb(offers_slim_mat row) is
--    byte-identical to to_jsonb(offers row) - 'specs' - 'gsm_url' (same columns,
--    same order), so the web payload shape is unchanged.
create materialized view if not exists offers_slim_mat as
  select variant_key, model, storage, ram, site, name, url, image_url,
         price, availability, condition, rating, review_count,
         warranty_days, warranty_label, condition_url,
         store_name, logo_url, default_warranty_days, trust_score,
         in_stock, last_seen_at
  from offers;

-- 2. specs-by-model snapshot = the distinct-on-per-model set specs_by_model_json
--    aggregates. lmodel is unique -> unique index enables a future concurrent
--    refresh; harmless for the plain refresh used now.
create materialized view if not exists specs_by_model_mat as
  select distinct on (lower(sx.model))
         lower(sx.model) as lmodel, sx.specs, sx.gsm_url
  from specs sx
  order by lower(sx.model),
           (sx.specs is not null) desc,
           (coalesce(sx.image_url, sx.image_fallback) is not null) desc,
           sx.updated_at desc;
create unique index if not exists specs_by_model_mat_pk on specs_by_model_mat (lmodel);

-- 3. Repoint the RPCs at the snapshots (same output shape as before).
create or replace function public.all_offers_json()
 returns jsonb language sql stable security definer
 set search_path to 'public' set statement_timeout to '120s'
as $function$
  select coalesce(
           jsonb_agg(to_jsonb(o) order by o.variant_key, o.site, o.condition),
           '[]'::jsonb)
  from offers_slim_mat o;
$function$;

create or replace function public.specs_by_model_json()
 returns jsonb language sql stable security definer
 set search_path to 'public' set statement_timeout to '120s'
as $function$
  select coalesce(
           jsonb_agg(jsonb_build_object('model', lmodel, 'specs', specs, 'gsm_url', gsm_url)
                     order by lmodel),
           '[]'::jsonb)
  from specs_by_model_mat;
$function$;

-- 4. FINAL definition of refresh_latest_prices() — refreshes ALL FOUR snapshots.
--    latest_prices_mat first (offers_slim_mat is built from the offers view, which
--    joins latest_prices_mat). Plain (non-concurrent) REFRESH because it runs in
--    the txn PostgREST wraps an rpc() in; 600s timeout so the offers refresh (~7s
--    for all four) is never cancelled. The scraper already calls this at the end
--    of normalize_db, so all snapshots stay as-fresh-as the scrape with no edit.
create or replace function public.refresh_latest_prices()
  returns void language plpgsql security definer
  set search_path = public set statement_timeout = '600s'
as $function$
begin
  refresh materialized view latest_prices_mat;
  refresh materialized view variant_price_extremes_mat;
  refresh materialized view offers_slim_mat;
  refresh materialized view specs_by_model_mat;
end;
$function$;

grant execute on function public.refresh_latest_prices() to service_role;
