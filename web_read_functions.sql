-- ---------------------------------------------------------------------------
-- Web read helpers: fetch the whole catalog for the Next.js app in a way that
-- doesn't OOM the database.
--
-- WHY (OOM crash-loop incident 2026-07-16): the web builds every page from the
-- `offers` view (~12-15k rows). It used to page through it in 1000-row selects
-- (PostgREST cap) — ~15 requests each re-running the full join. A single-call
-- RPC (`all_offers_json`) replaced that, BUT the `offers` view's LEFT JOIN
-- LATERAL duplicates the large per-model `specs` JSONB onto EVERY one of a
-- model's ~6 offer rows, so aggregating specs into one jsonb produced a ~60MB
-- blob materialized in DB memory per call. On an undersized compute instance
-- that drove Postgres into an OOM crash-loop (repeated automatic-recovery →
-- FATAL "not accepting connections" → Cloudflare 521/503; the management API
-- still showed ACTIVE_HEALTHY — the signal was in the postgres LOGS).
--
-- FIX: (A) bump the Supabase compute add-on (more RAM). (B) DECOUPLE specs from
-- offers — specs are per-MODEL (~2.1k rows), not per-offer (~12.6k rows):
--   * all_offers_json() returns the offers set WITHOUT specs/gsm_url (~10MB).
--   * specs_by_model_json() returns specs ONCE per model (~5.5MB), keyed by
--     lower(model), picking the same best-row-per-model the offers lateral does.
-- The web (lib/queries.js buildVariantCards) fetches both and joins specs onto
-- each card by lower(model). image_url stays resolved on the offer row.
-- Total DB payload 60MB -> ~16MB, with no 60MB memory spike.
--
-- Both are SECURITY DEFINER with their own statement_timeout so the full
-- aggregation isn't cut by the web role's 8s statement_timeout. Idempotent.
-- Apply AFTER specs_schema.sql (they read the `offers` view + `specs` table).
-- ---------------------------------------------------------------------------

-- Lean offers: the whole set as one jsonb row (bypasses PostgREST's 1000-row
-- cap), MINUS the per-row specs/gsm_url blobs (fetched per-model separately).
create or replace function all_offers_json()
returns jsonb
language sql
stable
security definer
set search_path = public
set statement_timeout = '120s'
as $$
  select coalesce(
           jsonb_agg((to_jsonb(o) - 'specs' - 'gsm_url')
                     order by o.variant_key, o.site, o.condition),
           '[]'::jsonb)
  from offers o;
$$;

grant execute on function all_offers_json() to anon, authenticated, service_role;

-- Specs once per model, keyed by lower(model). DISTINCT ON picks the same "best"
-- row per model as the offers view's LEFT JOIN LATERAL (specs non-null first,
-- then image non-null, then newest).
create or replace function specs_by_model_json()
returns jsonb
language sql
stable
security definer
set search_path = public
set statement_timeout = '120s'
as $$
  select coalesce(
           jsonb_agg(jsonb_build_object('model', lmodel, 'specs', specs, 'gsm_url', gsm_url)
                     order by lmodel),
           '[]'::jsonb)
  from (
    select distinct on (lower(sx.model))
           lower(sx.model) as lmodel, sx.specs, sx.gsm_url
    from specs sx
    order by lower(sx.model),
             (sx.specs is not null) desc,
             (coalesce(sx.image_url, sx.image_fallback) is not null) desc,
             sx.updated_at desc
  ) t;
$$;

grant execute on function specs_by_model_json() to anon, authenticated, service_role;
