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

from normalize import clean_model, make_variant_key, is_phone, set_dynamic_colors
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
                 .select("id, model, storage, ram, variant_key, site, name, in_stock, last_seen_at")
                 .order("id").range(start, start + 999).execute().data)
        rows += chunk
        if len(chunk) < 1000:
            break
        start += 1000
    return rows


# Words that read like performance/edition MODEL lines, not colours — kept out
# of the auto vocab even though they appear in some "Colors" strings, so a future
# model named after one isn't shortened before it lands in the DB.
_COLOR_DENY = {
    "legend", "meta", "racing", "sonic", "supersonic", "nitro", "viva",
    "phoenix", "maverick", "turbo", "speed", "power", "prime", "edition",
    "product", "gradient", "gradation", "and", "the", "with",
}


def build_color_vocab(phones):
    """Auto-grow the colour strip vocab from data we already have.

    Every phone's Beebom 'Colors' spec lists its colour names; collect the
    distinct colour WORDS across all specs, then SUBTRACT every token that is
    also part of a real model name (brand/series/model). What remains is a word
    that is a colour and never a model line, so stripping it from a model name
    can only fix a colour leak — never shorten a real model. Registered into
    normalize via set_dynamic_colors() so Pass 1 strips them. As new models get
    Beebom specs, their colours auto-enter this set on the next run.
    """
    # Protect: any token that appears in a real model name.
    mtokens = set()
    for p in phones:
        for tok in re.split(r"\s+", (p.get("model") or "").lower()):
            if tok:
                mtokens.add(tok)

    # Colour words from every Beebom specs row (paginate past the 1000-row cap).
    cwords, start = set(), 0
    while True:
        chunk = (sb.table("specs").select("specs")
                 .order("variant_key").range(start, start + 999).execute().data) or []
        for row in chunk:
            spec = row.get("specs")
            if not isinstance(spec, dict):
                continue
            for grp in spec.get("_groups") or []:
                if (grp.get("title") or "").strip().lower() != "general":
                    continue
                for label, val in (grp.get("rows") or []):
                    if str(label).strip().lower() not in ("colors", "colours"):
                        continue
                    for w in re.split(r"[,/&]|\s+", str(val).lower()):
                        w = w.strip()
                        if len(w) >= 3 and not any(c.isdigit() for c in w):
                            cwords.add(w)
        if len(chunk) < 1000:
            break
        start += 1000

    vocab = {w for w in cwords if w not in mtokens and w not in _COLOR_DENY}
    set_dynamic_colors(vocab)
    print(f"Auto colour vocab: {len(cwords)} colour words, "
          f"{len(vocab)} kept after removing model tokens + deny-list.")
    return vocab


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


def pass2_delete_stale_orphans(phones):
    """Delete STALE OUT-OF-STOCK duplicate rows left behind when a name
    normalization changed a row's saved name (so save_phone, keyed by raw name,
    created a new row and orphaned the old). Signature: an OOS row whose
    (site, variant_key) has a MORE-RECENTLY-seen row, AND that is redundant —
    its RAM is null OR a fresher sibling has the same RAM. The redundant-RAM
    guard PROTECTS legit distinct-RAM OOS rows at RAM-folding stores (oldsold/
    itradeit/samsungcr): a 12GB row going OOS while an 8GB stays in stock is NOT
    a duplicate, so it's kept. Run AFTER Pass 1 so variant_keys are already clean
    (that's what makes the orphan share its live twin's key)."""
    from collections import defaultdict
    print("\n── Pass 2: deleting stale OOS duplicate rows ──")
    norm_ram = lambda r: ((r or "").lower().replace(" ", "").replace("ram", "") or None)
    ls = lambda p: p.get("last_seen_at") or ""  # ISO strings compare lexically
    groups = defaultdict(list)
    for p in phones:
        groups[(p.get("site"), p.get("variant_key"))].append(p)
    deleted = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        newest = max(ls(p) for p in rows)
        for p in rows:
            if p.get("in_stock") or ls(p) >= newest or not ls(p):
                continue  # in stock, or it IS the newest -> keep
            pr = norm_ram(p.get("ram"))
            redundant = pr is None or any(
                ls(q) > ls(p) and norm_ram(q.get("ram")) == pr for q in rows)
            if not redundant:
                continue  # a legit distinct-RAM OOS variant -> keep
            pid = p["id"]
            try:
                sb.table("prices").delete().eq("phone_id", pid).execute()
                sb.table("phones").delete().eq("id", pid).execute()
                deleted += 1
            except Exception as e:
                print(f"  Delete error for {pid}: {e}")
    print(f"Pass 2 complete: {deleted} stale OOS duplicates deleted")
    return deleted


def normalize():
    print("Fetching all phones from DB...")
    phones = fetch_phones()
    print(f"Total phones: {len(phones)}")

    deleted = pass0_delete_nonphones(phones)
    if deleted:
        phones = fetch_phones()
        print(f"Phones remaining after cleanup: {len(phones)}")

    # Auto-grow the colour strip vocab from Beebom specs (minus model tokens)
    # BEFORE Pass 1 so the re-clean uses it. Best-effort: a failure here must not
    # break normalization (static COLORS still applies).
    try:
        build_color_vocab(phones)
    except Exception as e:
        log_error(e, component="normalize_db", phase="build_color_vocab")
        print(f"build_color_vocab failed ({e}); continuing with static COLORS only.")

    pass1_renormalize(phones)
    # Re-fetch so Pass 2 dedups on the freshly-recomputed variant_keys.
    pass2_delete_stale_orphans(fetch_phones())
    print("\n✓ Normalization complete.")


if __name__ == "__main__":
    init_sentry("normalize_db")
    try:
        normalize()
    except Exception as e:
        log_error(e, component="normalize_db", phase="normalize")
        raise
