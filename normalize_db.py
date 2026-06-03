"""
Deterministic normalization pass (replaces the old AI-based normalize_ai.py).

The scrapers already run every name through clean_model()/make_variant_key() at
save time, so a fresh scrape is self-correcting. This pass exists to:
  Pass 0 — DELETE non-phones using the deterministic is_phone() keyword check.
  Pass 1 — re-run clean_model()/make_variant_key() over every row so existing
           data picks up normalization-rule improvements without waiting for a
           store to be re-scraped (model + variant_key are recomputed in place;
           the raw site `name` is left untouched per the storage convention).

Cross-store duplicate merging (the old Pass 2 / canonical_key) is no longer an
automated step: make_variant_key is model+storage-only and deterministic, so the
same physical phone already shares one variant_key across stores once names are
clean. canonical_key stays available for the manual merge fallback (see CLAUDE.md)
and the offers view still reads coalesce(canonical_key, variant_key).

Run AFTER all scrapers complete.  Usage: python3 normalize_db.py
"""
import os
from dotenv import load_dotenv
from supabase import create_client

from normalize import clean_model, make_variant_key, is_phone
from obs import init_sentry, log_error

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_phones():
    """Page through ALL phones — PostgREST caps a single select at 1000 rows, so
    a plain query silently drops everything past the first page."""
    rows, start = [], 0
    while True:
        chunk = (sb.table("phones")
                 .select("id, model, storage, ram, variant_key, site, name")
                 .order("id").range(start, start + 999).execute().data)
        rows += chunk
        if len(chunk) < 1000:
            break
        start += 1000
    return rows


def pass0_delete_nonphones(phones):
    """Delete rows whose raw name/model is clearly an accessory or non-phone."""
    print("\n── Pass 0: deleting non-phones ──")
    deleted = 0
    for p in phones:
        name = p.get("name") or p.get("model") or ""
        if is_phone(name):
            continue
        pid = p["id"]
        try:
            sb.table("prices").delete().eq("phone_id", pid).execute()
            sb.table("phones").delete().eq("id", pid).execute()
            print(f"  Deleted: id={pid} ({name})")
            deleted += 1
        except Exception as e:
            print(f"  Delete error for {pid}: {e}")
    print(f"Pass 0 complete: {deleted} non-phones deleted")
    return deleted


def pass1_renormalize(phones):
    """Recompute model + variant_key for every row from its raw name."""
    print("\n── Pass 1: re-normalizing model names + keys ──")
    updated = 0
    for p in phones:
        raw = p.get("name") or p.get("model") or ""
        new_model = clean_model(raw)
        if not new_model:
            continue
        new_key = make_variant_key(new_model, p.get("storage"), p.get("ram"))
        patch = {}
        if new_model != p.get("model"):
            patch["model"] = new_model
        if new_key != p.get("variant_key"):
            patch["variant_key"] = new_key
        if not patch:
            continue
        try:
            sb.table("phones").update(patch).eq("id", p["id"]).execute()
            updated += 1
            if "model" in patch:
                print(f"  id={p['id']}: {p.get('model')!r} -> {new_model!r}")
        except Exception as e:
            print(f"  Update error for {p['id']}: {e}")
    print(f"Pass 1 complete: {updated} rows re-normalized")
    return updated


def normalize():
    print("Fetching all phones from DB...")
    phones = fetch_phones()
    print(f"Total phones: {len(phones)}")

    deleted = pass0_delete_nonphones(phones)
    if deleted:
        phones = fetch_phones()
        print(f"Phones remaining after cleanup: {len(phones)}")

    pass1_renormalize(phones)
    print("\n✓ Normalization complete.")


if __name__ == "__main__":
    init_sentry("normalize_db")
    try:
        normalize()
    except Exception as e:
        log_error(e, component="normalize_db", phase="normalize")
        raise
