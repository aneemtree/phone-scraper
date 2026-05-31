"""
AI-powered normalization pass using Claude API.

Three passes:
  Pass 0 — identify and DELETE non-phones (accessories, tablets, laptops, etc.)
  Pass 1 — clean model names (strip store names, noise words)
  Pass 2 — group by cleaned name, set canonical_key for cross-store duplicates

Run AFTER all scrapers complete.
Usage: python3 normalize_ai.py
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


def call_claude(prompt, max_tokens=2000):
    """Call Claude API and return parsed JSON response."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    r = requests.post(CLAUDE_URL, headers=headers, json=body, timeout=60)
    if r.status_code != 200:
        print(f"  Claude API error {r.status_code}: {r.text[:200]}")
        return None
    text = r.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}\n  Response: {text[:300]}")
        return None


# ─────────────────────────────────────────────
# PASS 0 — Delete non-phones
# ─────────────────────────────────────────────

def pass0_delete_nonphones(phones):
    """Ask Claude to identify non-phones and delete them from DB."""
    print("\n── Pass 0: Identifying non-phones ──")
    deleted = 0

    for i in range(0, len(phones), BATCH_SIZE):
        batch = phones[i:i + BATCH_SIZE]
        entries = [f"id={p['id']} site={p['site']} model={p['model']} storage={p['storage']}"
                   for p in batch]

        prompt = f"""You are reviewing a refurbished phone database.

Here are {len(batch)} products:

{chr(10).join(entries)}

Return a JSON array of IDs for products that are CLEARLY NOT smartphones. Only flag something if you are 100% certain it is not a phone — for example:
- "Power Bank 20000mAh" — clearly not a phone
- "Photography Kit" — clearly not a phone  
- "USB Cable" — clearly not a phone
- "Smart Watch" — clearly not a phone

DO NOT flag phones you don't recognise. If uncertain, do NOT include the ID.
If all products are phones (or you are unsure), return: []

CRITICAL: Return ONLY a valid JSON array of integers. No text before or after. No explanation.
Example response: [123, 456]
Empty response: []"""

        result = call_claude(prompt, max_tokens=500)
        if result is None:
            continue

        ids_to_delete = [int(x) for x in result if str(x).isdigit()]
        if ids_to_delete:
            for did in ids_to_delete:
                try:
                    sb.table("prices").delete().eq("phone_id", did).execute()
                    sb.table("phones").delete().eq("id", did).execute()
                    model = next((p['model'] for p in batch if p['id'] == did), did)
                    print(f"  Deleted: id={did} ({model})")
                    deleted += 1
                except Exception as e:
                    print(f"  Delete error for {did}: {e}")

        if i + BATCH_SIZE < len(phones):
            time.sleep(0.5)

    print(f"Pass 0 complete: {deleted} non-phones deleted")
    return deleted


# ─────────────────────────────────────────────
# PASS 1 — Clean model names
# ─────────────────────────────────────────────

def pass1_clean_names(phones):
    """Ask Claude to clean model names — strip store names, noise words, fix casing."""
    print("\n── Pass 1: Cleaning model names ──")
    updated = 0

    for i in range(0, len(phones), BATCH_SIZE):
        batch = phones[i:i + BATCH_SIZE]
        entries = [f"id={p['id']} site={p['site']} model={p['model']} storage={p['storage']}"
                   for p in batch]

        prompt = f"""You are cleaning model names in a refurbished phone database with listings from Indian stores (ControlZ, Cashify, Refit, Xtracover, SahiValue).

Here are {len(batch)} phone listings:

{chr(10).join(entries)}

Clean the model names:
- Remove store names: controlz, cashify, refit, xtracover, sahivalue
- Remove noise words: Refurbished, Renewed, Pre-owned, Open Box, Certified, Special Series
- Fix brand casing: iPhone (not Iphone), iPad, OnePlus, POCO, iQOO, Samsung, Google
- Keep model numbers, variants (Pro, Plus, Max, Ultra, FE, Lite), and 5G/4G designations
- Do NOT change correct names

Return ONLY a JSON array. Each item:
- "id": phone id (number)
- "model": corrected name (same if already correct)

Return ONLY the JSON array, no explanation."""

        result = call_claude(prompt)
        if not result:
            print(f"  Batch {i//BATCH_SIZE + 1}: skipped")
            continue

        originals = {str(p["id"]): p for p in batch}
        batch_updated = 0
        for item in result:
            pid = str(item.get("id", ""))
            if pid not in originals:
                continue
            orig = originals[pid]
            new_model = (item.get("model") or "").strip()
            if new_model and new_model != orig["model"]:
                storage = orig.get("storage") or ""
                patch = {"model": new_model, "name": f"{new_model} {storage}".strip()}
                try:
                    sb.table("phones").update(patch).eq("id", int(pid)).execute()
                    updated += 1
                    batch_updated += 1
                except Exception as e:
                    if "23505" in str(e):
                        # Duplicate exists with correct name — delete this stale row
                        try:
                            sb.table("prices").delete().eq("phone_id", int(pid)).execute()
                            sb.table("phones").delete().eq("id", int(pid)).execute()
                            print(f"  Merged duplicate: id={pid} ({orig['model']} -> {new_model})")
                        except Exception as e2:
                            print(f"  Merge error for {pid}: {e2}")
                    else:
                        print(f"  Update error for {pid}: {e}")

        print(f"  Batch {i//BATCH_SIZE + 1}/{(len(phones)-1)//BATCH_SIZE + 1}: {batch_updated} names cleaned")
        if i + BATCH_SIZE < len(phones):
            time.sleep(0.5)

    print(f"Pass 1 complete: {updated} model names cleaned")
    return updated


# ─────────────────────────────────────────────
# PASS 2 — Set canonical keys for cross-store duplicates
# ─────────────────────────────────────────────

def pass2_canonical_keys(phones):
    """Group phones by cleaned model+storage, ask Claude to confirm duplicates and set canonical keys."""
    print("\n── Pass 2: Setting canonical keys ──")
    import re
    from collections import defaultdict

    def rough_key(p):
        """Group key using cleaned model + storage."""
        m = re.sub(r"[^a-z0-9]", "", (p.get("model") or "").lower())
        st = re.sub(r"[^a-z0-9]", "", (p.get("storage") or "").lower())
        return m + "_" + st

    # Group phones by rough key
    groups = defaultdict(list)
    for p in phones:
        groups[rough_key(p)].append(p)

    # Only process groups with phones from multiple stores (these need canonical keys)
    multi_store_groups = {k: v for k, v in groups.items()
                          if len(set(p["site"] for p in v)) > 1}

    print(f"  Groups with multiple stores: {len(multi_store_groups)}")

    # Pack into batches keeping same-model phones together
    batches, current = [], []
    for group in multi_store_groups.values():
        if current and len(current) + len(group) > BATCH_SIZE:
            batches.append(current)
            current = []
        if len(group) > BATCH_SIZE:
            for i in range(0, len(group), BATCH_SIZE):
                batches.append(group[i:i + BATCH_SIZE])
        else:
            current.extend(group)
    if current:
        batches.append(current)

    updated = 0
    for i, batch in enumerate(batches, 1):
        entries = [f"id={p['id']} site={p['site']} model={p['model']} storage={p['storage']} ram={p['ram']}"
                   for p in batch]

        prompt = f"""You are merging duplicate phone listings from different refurbished phone stores.

Here are {len(batch)} phone listings that may be duplicates across stores:

{chr(10).join(entries)}

Rules:
- Same phone = same model + same storage capacity. Different storage = different phone.
- Pro/Plus/Max/Ultra/FE variants are DIFFERENT phones — never group them together.
- If two or more phones are the same device from different stores, give ALL of them the same canonical_key.
- canonical_key format: brand-model_storage (lowercase, hyphens for spaces, underscore before storage)
  Examples: apple-iphone-11_64gb, samsung-galaxy-s23_256gb, oneplus-nord-2t_128gb
- If a phone has no duplicate in this batch, set canonical_key to null.
- IMPORTANT: ALL phones in a duplicate group must get the same canonical_key — not just one.

Return ONLY a JSON array. Each item:
- "id": phone id (number, copy exactly)
- "canonical_key": shared key for duplicates, null if no duplicate

Return ONLY the JSON array, no explanation."""

        result = call_claude(prompt)
        if not result:
            print(f"  Batch {i}/{len(batches)}: skipped")
            continue

        originals = {str(p["id"]): p for p in batch}

        # Debug: show what Claude returned for first batch
        if i == 1:
            keys_set = [x for x in result if x.get("canonical_key")]
            print(f"  Debug batch 1: {len(result)} items, {len(keys_set)} with canonical keys")
            for x in keys_set[:3]:
                pid_d = str(x["id"])
                orig_key = originals.get(pid_d, {}).get("canonical_key")
                new_key = x["canonical_key"]
                match = new_key == orig_key
                print(f"    id={x['id']} existing={orig_key} -> new={new_key} (skip={match})")
        batch_updated = 0
        for item in result:
            pid = str(item.get("id", ""))
            if pid not in originals:
                continue
            new_canonical = item.get("canonical_key")
            if new_canonical and new_canonical != originals[pid].get("canonical_key"):
                try:
                    sb.table("phones").update({"canonical_key": new_canonical}).eq("id", int(pid)).execute()
                    updated += 1
                    batch_updated += 1
                except Exception as e:
                    print(f"  Update error for {pid}: {e}")

        print(f"  Batch {i}/{len(batches)}: {batch_updated} canonical keys set")
        if i < len(batches):
            time.sleep(0.5)

    print(f"Pass 2 complete: {updated} canonical keys set")
    return updated


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def normalize():
    print("Fetching all phones from DB...")
    phones = fetch_phones()
    print(f"Total phones: {len(phones)}")

    # Pass 0: delete non-phones
    deleted = pass0_delete_nonphones(phones)
    if deleted:
        phones = fetch_phones()
        print(f"Phones remaining after cleanup: {len(phones)}")

    # Pass 1: clean model names
    pass1_clean_names(phones)

    # Re-fetch with cleaned names for pass 2
    phones = fetch_phones()

    # Pass 2: set canonical keys
    pass2_canonical_keys(phones)

    print("\n✓ Normalization complete.")


if __name__ == "__main__":
    normalize()