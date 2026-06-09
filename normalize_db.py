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
import re
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


def load_alias_map():
    """{cleaned alias name (lower) -> cleaned canonical model} from model_aliases.
    Lets store name variations (e.g. 'iPhone SE 2' / 'SE 2nd Gen' / 'SE 2020')
    collapse to one canonical model, so they share a variant_key (one card)."""
    rev = {}
    try:
        rows = sb.table("model_aliases").select("model,alt_name_1,alt_name_2").execute().data or []
    except Exception as e:
        print(f"  (model_aliases not available: {e})")
        return rev
    for r in rows:
        canon = clean_model(r.get("model") or "")
        if not canon:
            continue
        for alt in (r.get("alt_name_1"), r.get("alt_name_2")):
            if alt:
                rev[clean_model(alt).lower()] = canon
    return rev


def pass1_renormalize(phones):
    """Recompute model + variant_key for every row from its raw name, applying the
    model_aliases canonical-name overrides."""
    print("\n── Pass 1: re-normalizing model names + keys ──")
    rev = load_alias_map()
    if rev:
        print(f"  loaded {len(rev)} alias -> canonical mappings")
    updated = 0
    for p in phones:
        raw = p.get("name") or p.get("model") or ""
        new_model = clean_model(raw)
        if not new_model:
            continue
        new_model = rev.get(new_model.lower(), new_model)   # alias override
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


# Variant-line words and brand/sub-brand words that are NEVER store-specific noise,
# so the consensus de-leak below must not strip them (Pro/Max/CE/Edge/FE are real
# different phones; brand words are identity).
_PROTECT = set((
    "pro max ultra plus lite neo turbo note fe mini fold flip edge ce air se prime "
    "power ace active master racing explorer champion speed gt zoom fusion stylus "
    "play prime nord narzo"
).split())
_BRANDS = set((
    "apple iphone ipad samsung galaxy xiaomi mi redmi poco vivo iqoo oppo realme "
    "narzo oneplus nord google pixel nothing cmf sony motorola moto asus honor "
    "huawei nokia lava infinix tecno micromax fairphone lg"
).split())


def pass2_consensus_deleak(phones):
    """Self-healing: strip a trailing colour/junk word from a model when a SHORTER
    base model is sold by MORE stores (consensus that the extra word is store-
    specific noise), e.g. 'Vivo V60e Noble' -> 'Vivo V60e'. Never strips variant-
    line words (Pro/Max/CE/Edge/FE/...) or brand words, and only when the base is
    strictly more widespread — so real variants are safe. No colour list to maintain.
    """
    print("\n── Pass 2: consensus de-leak (strip store-specific colour/junk) ──")
    model_sites = {}
    for p in phones:
        m = (p.get("model") or "").strip()
        if m:
            model_sites.setdefault(m, set()).add(p.get("site"))
    models = set(model_sites)

    def base_for(model):
        toks = model.split()
        for drop in (1, 2):
            if len(toks) - drop < 2:            # keep at least brand + one token
                break
            tail = [t.lower() for t in toks[-drop:]]
            if not all(re.fullmatch(r"[a-z]+", t) for t in tail):   # only pure-alpha words
                continue
            if any(t in _PROTECT or t in _BRANDS for t in tail):
                continue
            base = " ".join(toks[:-drop])
            if base in models and len(model_sites[base]) > len(model_sites[model]):
                return base
        return None

    remap = {m: base_for(m) for m in models}
    remap = {m: b for m, b in remap.items() if b}
    updated = 0
    for p in phones:
        b = remap.get(p.get("model"))
        if not b:
            continue
        patch = {"model": b}
        new_key = make_variant_key(b, p.get("storage"), p.get("ram"))
        if new_key != p.get("variant_key"):
            patch["variant_key"] = new_key
        try:
            sb.table("phones").update(patch).eq("id", p["id"]).execute()
            updated += 1
        except Exception as e:
            print(f"  Update error for {p['id']}: {e}")
    for m, b in sorted(remap.items()):
        print(f"  de-leak: {m!r} -> {b!r}")
    print(f"Pass 2 complete: {updated} rows de-leaked ({len(remap)} models)")
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
    phones = fetch_phones()              # re-fetch so Pass 2 sees Pass 1's models
    pass2_consensus_deleak(phones)
    print("\n✓ Normalization complete.")


if __name__ == "__main__":
    init_sentry("normalize_db")
    try:
        normalize()
    except Exception as e:
        log_error(e, component="normalize_db", phase="normalize")
        raise
