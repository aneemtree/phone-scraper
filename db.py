"""
Database helper. Connects to Supabase and saves phones + price snapshots.
All scrapers import from here.
"""
import os
from supabase import create_client, Client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def save_phone(site, name, url, image_url, model, storage, ram, variant_key):
    """
    Insert a phone offer if new for this (site, name), else return existing id.
    'name' stays the raw site title; model/storage/ram/variant_key are normalized.
    """
    existing = (
        supabase.table("phones")
        .select("id")
        .eq("site", site)
        .eq("name", name)
        .execute()
    )
    if existing.data:
        pid = existing.data[0]["id"]
        supabase.table("phones").update({
            "url": url, "image_url": image_url, "model": model,
            "storage": storage, "ram": ram, "variant_key": variant_key,
        }).eq("id", pid).execute()
        return pid

    inserted = supabase.table("phones").insert({
        "site": site, "name": name, "url": url, "image_url": image_url,
        "model": model, "storage": storage, "ram": ram, "variant_key": variant_key,
    }).execute()
    return inserted.data[0]["id"]


def save_price(phone_id, price, availability="in_stock", condition="Premium Renewed",
               rating=None, review_count=None, warranty_months=None):
    """Append a price snapshot (one per condition). Never overwrites; history accrues."""
    supabase.table("prices").insert({
        "phone_id": phone_id, "price": price, "availability": availability,
        "condition": condition, "rating": rating, "review_count": review_count,
        "warranty_months": warranty_months,
    }).execute()


# ---------- Image self-hosting (first sighting only) ----------
import requests as _requests

BUCKET = "phone-images"


def storage_public_url(path):
    """Build the public URL for a file in our Supabase Storage bucket."""
    base = SUPABASE_URL.rstrip("/")
    return f"{base}/storage/v1/object/public/{BUCKET}/{path}"


def ensure_image(source_url, dest_path):
    """Download source_url and upload to Storage at dest_path, but only if we
    don't already have it. Returns our public URL (or None on failure).
    First-sighting-only: if the file already exists, we skip the download."""
    if not source_url:
        return None
    # 1) Already stored? Check by trying to list/head the object.
    try:
        existing = supabase.storage.from_(BUCKET).list(
            path="/".join(dest_path.split("/")[:-1]) or None
        )
        fname = dest_path.split("/")[-1]
        if any(o.get("name") == fname for o in (existing or [])):
            return storage_public_url(dest_path)
    except Exception:
        pass  # if listing fails, fall through and try to upload

    # 2) Download the image bytes
    try:
        r = _requests.get(source_url, timeout=30)
        r.raise_for_status()
        data = r.content
        content_type = r.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        print(f"    image download failed: {e}")
        return None

    # 3) Upload to Supabase Storage
    try:
        supabase.storage.from_(BUCKET).upload(
            dest_path, data, {"content-type": content_type, "upsert": "false"}
        )
    except Exception as e:
        # If it already exists (race), treat as success.
        if "exists" in str(e).lower() or "duplicate" in str(e).lower():
            return storage_public_url(dest_path)
        print(f"    image upload failed: {e}")
        return None
    return storage_public_url(dest_path)