"""
Database helper. Connects to Supabase and saves phones + price snapshots.
All scrapers import from here.
"""
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

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
    now = _utcnow_iso()
    if existing.data:
        pid = existing.data[0]["id"]
        # Seeing the phone this run = in stock; stamp last_seen_at for the OOS sweep.
        supabase.table("phones").update({
            "url": url, "image_url": image_url, "model": model,
            "storage": storage, "ram": ram, "variant_key": variant_key,
            "last_seen_at": now, "in_stock": True,
        }).eq("id", pid).execute()
        return pid

    inserted = supabase.table("phones").insert({
        "site": site, "name": name, "url": url, "image_url": image_url,
        "model": model, "storage": storage, "ram": ram, "variant_key": variant_key,
        "last_seen_at": now, "in_stock": True,
    }).execute()
    return inserted.data[0]["id"]


def save_price(phone_id, price, availability="in_stock", condition="Premium Renewed",
               rating=None, review_count=None, warranty_months=None, url=None):
    """Append a price snapshot (one per condition). Never overwrites; history accrues."""
    supabase.table("prices").insert({
        "phone_id": phone_id, "price": price, "availability": availability,
        "condition": condition, "rating": rating, "review_count": review_count,
        "warranty_months": warranty_months, "url": url,
    }).execute()



def mark_site_oos(site):
    """Delete today's prices for this site before scraping.
    Called at the start of each scraper run so stale in_stock prices are cleared.
    Only phones found in the current scrape will have prices — others show as unavailable.
    Keeps price history older than today intact for trend tracking.
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Get all phone IDs for this site
    phones = supabase.table("phones").select("id").eq("site", site).execute()
    phone_ids = [p["id"] for p in (phones.data or [])]
    if not phone_ids:
        return 0
    deleted = 0
    for i in range(0, len(phone_ids), 100):
        batch_ids = phone_ids[i:i+100]
        for pid in batch_ids:
            supabase.table("prices").delete().eq("phone_id", pid).gte("scraped_at", today).execute()
            deleted += 1
    return deleted


def mark_unseen_out_of_stock(site, run_started_at, min_seen_ratio=0.5):
    """Flag this site's phones that were NOT seen during the run as out of stock.

    Call at the END of a scraper, passing the timestamp captured at the START of
    the run. save_phone() stamps last_seen_at=now on every phone it sees, so any
    phone with last_seen_at < run_started_at (or null) wasn't in this run.

    Guard: if fewer than `min_seen_ratio` of the site's phones were seen, the
    sweep is skipped — so a crashed or partial run can't wipe a whole store to
    out of stock. Phones found this run are set back in_stock=true by save_phone.
    """
    total = (supabase.table("phones").select("id", count="exact")
             .eq("site", site).execute().count or 0)
    if total == 0:
        return 0
    seen = (supabase.table("phones").select("id", count="exact")
            .eq("site", site).gte("last_seen_at", run_started_at).execute().count or 0)
    if seen < max(1, int(total * min_seen_ratio)):
        print(f"  [oos] {site}: only {seen}/{total} phones seen this run — skipping OOS sweep (guard)")
        return 0
    # Not seen this run -> out of stock (older last_seen_at, or never stamped).
    supabase.table("phones").update({"in_stock": False}).eq("site", site).lt("last_seen_at", run_started_at).execute()
    supabase.table("phones").update({"in_stock": False}).eq("site", site).is_("last_seen_at", "null").execute()
    still_in = (supabase.table("phones").select("id", count="exact")
                .eq("site", site).eq("in_stock", True).execute().count or 0)
    n = total - still_in
    # Per-condition availability: a phone can be in stock while one of its grades
    # is sold out. Mark grades not refreshed this run as out_of_stock too.
    cond_oos = _mark_disappeared_conditions_oos(site, run_started_at)
    print(f"  [oos] {site}: {seen}/{total} seen, {n} phones OOS, {cond_oos} conditions OOS")
    return n


def _mark_disappeared_conditions_oos(site, run_started_at):
    """For phones SEEN this run, mark any condition NOT refreshed this run as
    out of stock by appending an out_of_stock price snapshot (copying the last
    known price/url). latest_prices then surfaces out_of_stock for that grade,
    so a sold-out condition stops showing as in stock while its sibling grades
    stay available. Idempotent: skips conditions whose latest row is already OOS.
    """
    seen = (supabase.table("phones").select("id")
            .eq("site", site).gte("last_seen_at", run_started_at).execute().data or [])
    seen_ids = [p["id"] for p in seen]
    inserted = 0
    for i in range(0, len(seen_ids), 50):
        batch = seen_ids[i:i + 50]
        # (phone, condition) combos that got a fresh row this run (server-side
        # timestamp compare avoids string-format pitfalls).
        fresh = {(r["phone_id"], r["condition"]) for r in
                 (supabase.table("prices").select("phone_id, condition")
                  .in_("phone_id", batch).gte("scraped_at", run_started_at).execute().data or [])}
        all_rows = (supabase.table("prices")
                    .select("phone_id, condition, price, availability, scraped_at, url")
                    .in_("phone_id", batch).execute().data or [])
        # latest row per (phone, condition) — same-format DB timestamps, string max ok.
        latest = {}
        for r in all_rows:
            k = (r["phone_id"], r["condition"])
            cur = latest.get(k)
            if cur is None or r["scraped_at"] > cur["scraped_at"]:
                latest[k] = r
        for k, r in latest.items():
            if k in fresh or r["availability"] == "out_of_stock":
                continue
            supabase.table("prices").insert({
                "phone_id": r["phone_id"], "price": r["price"],
                "availability": "out_of_stock", "condition": r["condition"],
                "url": r.get("url"),
            }).execute()
            inserted += 1
    return inserted


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