-- AI normalizer review queue (normalize_ai.py).
--
-- The AI pass is PROPOSE-ONLY: it never writes to phones.model / variant_key.
-- Every suggestion lands here for manual review. Promote a good one by hand:
--   strip/keep  -> add the rule to clean_model() (or just delete the row if the
--                  deterministic + GSMArena passes already cover it)
--   alias       -> insert into model_aliases (model, alt_name_1) and let
--                  normalize_db.py Pass 1 apply it on the next run
-- then mark the review row resolved (status='applied' / 'rejected').
--
-- Idempotent: safe to re-run. A given (name, proposed_action, target) is unique
-- so re-runs update confidence/reason in place instead of piling duplicates.

create table if not exists normalize_review (
    id              bigint generated always as identity primary key,
    name            text not null,            -- the current cleaned model in our DB
    proposed_action text not null,            -- 'strip' | 'alias' | 'keep'
    target          text,                     -- strip/alias: the resolved canonical name; keep: null
    confidence      numeric,                  -- 0..1 from the model
    reason          text,                     -- one-line justification
    sample_sites    text,                     -- which stores carry `name` (context)
    gsm_context     text,                     -- GSMArena candidates shown to the model
    status          text not null default 'pending',   -- pending | applied | rejected
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (name, proposed_action, target)
);

create index if not exists normalize_review_status_idx on normalize_review (status);

-- keep updated_at fresh on re-proposal
create or replace function set_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists normalize_review_updated_at on normalize_review;
create trigger normalize_review_updated_at before update on normalize_review
    for each row execute function set_updated_at();
