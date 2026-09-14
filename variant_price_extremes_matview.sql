-- ---------------------------------------------------------------------------
-- variant_price_extremes_mat: materialize the per-variant all-time in-stock
-- price HIGH/LOW so the web stops aggregating the whole append-only `prices`
-- history on every render.
--
-- WHY: the web's getVariantExtremes() calls the variant_price_extremes() RPC to
-- attach `high` (all-time in-stock high) + `discountPct`/`dealScore` to every
-- card — this drives the -X% badge, the DEFAULT best-deal ordering everywhere,
-- and the dedicated Top Deals page. The RPC used to `max(price)/min(price)` over
-- the entire `prices` table (200k+ rows, growing) grouped by variant on EVERY
-- ISR regen — the same class of full-history scan that latest_prices_mat removed
-- from `offers`. It was also the load that contributed to the 2026-09 timeout
-- storm.
--
-- Two problems fixed together:
--   1) LOAD: the aggregate now runs at scrape cadence into a snapshot, not per
--      web request (the RPC becomes a ~800-row table scan, milliseconds).
--   2) TOP DEALS 1000-ROW CAP: the RPC returns one row per variant. When it
--      returned a row for EVERY variant that had ever been in stock (~1672) it
--      exceeded PostgREST's hard max-rows=1000 cap, so ~672 variants lost their
--      `high` -> discountPct=0 -> they never qualified for Top Deals. The
--      snapshot (and the RPC) is restricted to variants that CURRENTLY have an
--      in-stock phone row (~800, safely < 1000). The all-time high/low is still
--      computed over full in-stock history for those variants, so discount
--      percentages are unchanged for phones that actually appear on the site.
--
-- The snapshot is refreshed by the scraper's EXISTING end-of-pipeline
-- refresh_latest_prices() call (see below) — prices only change on a scrape — so
-- there is NO per-scraper edit and no new workflow step.
--
-- Idempotent. Apply AFTER latest_prices_matview.sql (this file redefines
-- refresh_latest_prices() to refresh BOTH snapshots, and references
-- latest_prices_mat which that file creates).
-- ---------------------------------------------------------------------------

-- Snapshot: all-time in-stock high/low, only for currently-in-stock variants.
create materialized view if not exists variant_price_extremes_mat as
  select coalesce(ph.canonical_key, ph.variant_key) as variant_key,
         max(p.price) as high,
         min(p.price) as low
  from prices p
  join phones ph on ph.id = p.phone_id
  where p.availability = 'in_stock' and p.price is not null
    and coalesce(ph.canonical_key, ph.variant_key) in (
      select coalesce(ph2.canonical_key, ph2.variant_key)
      from phones ph2 where ph2.in_stock = true)
  group by coalesce(ph.canonical_key, ph.variant_key);

-- Unique key: lets a pg_cron-driven `refresh ... concurrently` be used later if a
-- lock-free refresh is ever wanted; harmless for the plain refresh via the rpc.
create unique index if not exists variant_price_extremes_mat_vk
  on variant_price_extremes_mat (variant_key);

-- First populate (no-op once it has data).
refresh materialized view variant_price_extremes_mat;

-- Repoint the RPC at the snapshot. Same signature/columns, so the web app +
-- scripts/build-catalog-snapshot.mjs need no change. Row count stays < 1000, so
-- the full result is returned (no PostgREST truncation of Top Deals).
create or replace function public.variant_price_extremes()
  returns table(variant_key text, high numeric, low numeric)
  language sql
  stable
as $$
  select variant_key, high, low from variant_price_extremes_mat;
$$;

-- Extend refresh_latest_prices() (originally in latest_prices_matview.sql) to
-- refresh BOTH snapshots. The scraper already calls this rpc at the end of
-- normalize_db (after all price writes, before notify.py reads offers), so both
-- matviews stay as-fresh-as the scrape with NO scraper code change. Plain
-- (non-concurrent) REFRESH because CONCURRENTLY cannot run inside the txn
-- PostgREST wraps an rpc() in (same reason as latest_prices_matview.sql).
create or replace function refresh_latest_prices()
  returns void
  language plpgsql
  security definer
  set search_path = public
as $$
begin
  refresh materialized view latest_prices_mat;
  refresh materialized view variant_price_extremes_mat;
end;
$$;

grant execute on function refresh_latest_prices() to service_role;

-- NOTE: refresh_latest_prices() is redefined AGAIN in offers_slim_matview.sql to
-- ALSO refresh offers_slim_mat + specs_by_model_mat (four matviews total). Apply
-- that file AFTER this one; if you re-apply THIS file afterwards, re-apply
-- offers_slim_matview.sql too (else the function reverts to two refreshes).
