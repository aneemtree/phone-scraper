"""
AI-powered normalization pass using Claude API.

Runs AFTER all scrapers complete. Does two things in one pass per batch:
1. Cleans model names (strips store names, noise words the regex missed)
2. Groups cross-store duplicates and sets canonical_key

Usage:
    python3 normalize_ai.py

Requires ANTHROPIC_API_KEY in .env
"""
import os
import json
import time
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
BATCH_SIZE = 20


def fetch_phones():
    resp = sb.table("phones").select(
        "id, model, storage, ram, variant_key, canonical_key, site, name"
    ).order("model").execute()
    return resp.data


def ask_claude(phones_batch):
    """Send a batch of phones to Claude for name cleaning and duplicate detection.
    Returns a list of corrections."""

    entries = []
    for p in phones_batch:
        entries.append(
            f"id={p['id']} site={p['site']} model={p['model']!r} "
            f"storage={p['storage']} ram={p['ram']} variant_key={p['variant_key']}"
        )

    prompt = f"""You are cleaning a refurbished phone database with listings from multiple Indian stores (ControlZ, Cashify, Refit).

Here are {len(phones_batch)} phone listings:

{chr(10).join(entries)}

Do TWO things:

1. CLEAN MODEL NAMES: Fix any model names that contain store names, noise words, or are incorrect.
   Common issues: "Apple iPhone 11 Controlz Refurbished" → "Apple iPhone 11"
   Brand casing: iPhone, iPad, OnePlus, POCO, iQOO (not Iqoo or Poco)
   Only fix if the name is genuinely wrong — don't change correct names.

2. GROUP DUPLICATES: Find phones that are the SAME physical device sold by different stores.
   Same phone = same model + same storage. Different storage = different phone.
   Pro/Plus/Max variants are DIFFERENT phones — never group them together.
   Assign the same canonical_key to duplicates. Use format: brand-model_storage
   e.g. apple-iphone-11_64gb (lowercase, hyphens for spaces, underscore before storage)

IMPORTANT for canonical_key:
- If two or more phones are the same device from different stores, ALL of them must get the SAME canonical_key
- Do not leave one store's entry with null and another with a key — set the key on ALL duplicates
- canonical_key format: brand-model_storage e.g. apple-iphone-11_64gb, samsung-galaxy-s23_256gb

Respond with ONLY a JSON array. Each item must have:
- "id": the phone id (copy exactly as given, keep as number if number)
- "model": corrected model name (or same if already correct)
- "canonical_key": same canonical key for all duplicates of the same phone, null if no duplicate

Example:
[
  {{"id": 833, "model": "Apple iPhone 11", "canonical_key": "apple-iphone-11_128gb"}},
  {{"id": 778, "model": "Apple iPhone 11", "canonical_key": "apple-iphone-11_128gb"}},
  {{"id": 901, "model": "Apple iPhone 13 Pro", "canonical_key": null}}
]

Return ONLY the JSON array, no explanation, no markdown."""

    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }

    r = requests.post(CLAUDE_URL, headers=headers, json=body, timeout=60)
    if r.status_code != 200:
        print(f"  Claude API error {r.status_code}: {r.text[:200]}")
        return None

    text = r.json()["content"][0]["text"].strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        print(f"  Response was: {text[:300]}")
        return None


def apply_corrections(corrections, original_phones):
    """Apply model name and canonical_key corrections to DB."""
    # Build lookup of original phones by id
    originals = {str(p["id"]): p for p in original_phones}

    updated_model = 0
    updated_canonical = 0

    for c in corrections:
        pid = str(c.get("id", ""))
        if not pid or pid not in originals:
            continue

        orig = originals[pid]
        patch = {}

        # Model name changed?
        new_model = c.get("model", "").strip()
        if new_model and new_model != orig["model"]:
            patch["model"] = new_model
            # Also update name field
            storage = orig.get("storage") or ""
            patch["name"] = f"{new_model} {storage}".strip()
            updated_model += 1

        # Canonical key set?
        new_canonical = c.get("canonical_key")
        if new_canonical and new_canonical != orig.get("canonical_key"):
            patch["canonical_key"] = new_canonical
            updated_canonical += 1

        if patch:
            try:
                # id column is integer in DB — cast back from string
                db_id = int(pid) if str(pid).isdigit() else pid
                result = sb.table("phones").update(patch).eq("id", db_id).execute()
                if not result.data:
                    print(f"  Warning: update returned no data for id={db_id}")
            except Exception as e:
                print(f"  DB update error for {pid}: {e}")

    return updated_model, updated_canonical


def normalize():
    print("Fetching all phones from DB...")
    phones = fetch_phones()
    print(f"Total phones: {len(phones)}")

    # Group by cleaned model name so same-model phones from different stores
    # always appear in the same batch — essential for cross-store dedup.
    from collections import defaultdict
    import re as _re
    from normalize import clean_model as _clean_model

    def rough_model(p):
        """Use clean_model from normalize.py — the single source of truth
        for stripping noise. Storage appended to keep variants separate."""
        model_key = _re.sub(r"[^a-z0-9]", "", _clean_model(p.get("model") or "").lower())
        storage_key = _re.sub(r"[^a-z0-9]", "", (p.get("storage") or "").lower())
        return model_key + "_" + storage_key

    # Group phones by rough model key
    model_groups = defaultdict(list)
    for p in phones:
        model_groups[rough_model(p)].append(p)

    # Pack groups into batches of BATCH_SIZE, keeping same-model phones together
    batches, current_batch = [], []
    for group in model_groups.values():
        # If adding this group would exceed batch size and batch is non-empty, flush
        if current_batch and len(current_batch) + len(group) > BATCH_SIZE:
            batches.append(current_batch)
            current_batch = []
        # If a single group is larger than BATCH_SIZE, split it (rare)
        if len(group) > BATCH_SIZE:
            for i in range(0, len(group), BATCH_SIZE):
                batches.append(group[i:i + BATCH_SIZE])
        else:
            current_batch.extend(group)
    if current_batch:
        batches.append(current_batch)

    print(f"Processing {len(batches)} batches (grouped by model) covering {len(phones)} phones\n")

    total_model = 0
    total_canonical = 0

    for i, batch in enumerate(batches, 1):
        print(f"Batch {i}/{len(batches)} ({len(batch)} phones)...")
        corrections = ask_claude(batch)

        if not corrections:
            print(f"  Skipped (no response)")
            continue

        nm, nc = apply_corrections(corrections, batch)
        total_model += nm
        total_canonical += nc
        print(f"  ✓ {nm} model names fixed, {nc} canonical keys set")

        # Rate limiting — be nice to the API
        if i < len(batches):
            time.sleep(1)

    print(f"\nDone.")
    print(f"  Model names corrected: {total_model}")
    print(f"  Canonical keys set:    {total_canonical}")
    print(f"\nRun the website to see merged listings.")


if __name__ == "__main__":
    normalize()