"""
One-off: migrate existing phone images from Supabase Storage to Cloudflare R2
and rewrite phones.image_url to the R2 public URL.

Why: Supabase "cached egress" (serving images via its CDN) is the quota we blew.
R2 has zero egress, so we host + serve images there instead. The DB stays on
Supabase. New scrapes already upload to R2 via db.ensure_image(); this backfills
the images that were uploaded to Supabase before the switch.

Needs in the environment (same as the scrapers):
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL

Run:
    python3 migrate_images_to_r2.py            # dry run (prints what it would do)
    python3 migrate_images_to_r2.py --apply    # actually copy + rewrite URLs
"""
import sys
import requests
import db

# Marks a Supabase Storage public URL: ".../storage/v1/object/public/<bucket>/<key>"
SUPA_MARKER = "/storage/v1/object/public/"


def supabase_key(image_url):
    """Return the object key (path within the bucket) for a Supabase Storage URL,
    or None if this isn't a Supabase-hosted image."""
    if not image_url or SUPA_MARKER not in image_url:
        return None
    after = image_url.split(SUPA_MARKER, 1)[1]      # "<bucket>/<key...>"
    parts = after.split("/", 1)
    return parts[1] if len(parts) == 2 else None


def main(apply: bool):
    client = db._r2()
    if client is None:
        print("R2 not configured (need R2_ACCOUNT_ID/ACCESS_KEY/SECRET/BUCKET/PUBLIC_BASE_URL). Aborting.")
        return

    rows = (db.supabase.table("phones")
            .select("id, image_url")
            .execute().data or [])
    todo = [(r["id"], r["image_url"], supabase_key(r["image_url"])) for r in rows]
    todo = [(i, u, k) for (i, u, k) in todo if k]
    print(f"{len(rows)} phones; {len(todo)} have Supabase-hosted images to migrate.")

    migrated = skipped = failed = 0
    for pid, url, key in todo:
        new_url = db.r2_public_url(key)
        # Upload to R2 if not already there.
        try:
            client.head_object(Bucket=db.R2_BUCKET, Key=key)
            present = True
        except Exception:
            present = False
        if not present:
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                ct = resp.headers.get("Content-Type", "image/jpeg")
                if apply:
                    client.put_object(Bucket=db.R2_BUCKET, Key=key, Body=resp.content, ContentType=ct)
            except Exception as e:
                print(f"  FAIL id={pid} {key}: {str(e)[:80]}")
                failed += 1
                continue
        # Rewrite the DB url.
        if apply:
            db.supabase.table("phones").update({"image_url": new_url}).eq("id", pid).execute()
        migrated += 1
        if migrated <= 5 or migrated % 200 == 0:
            print(f"  {'migrated' if apply else 'would migrate'} id={pid}: {key} -> {new_url}")

    print(f"\n{'Migrated' if apply else 'Would migrate'} {migrated}, failed {failed}.")
    if not apply:
        print("Dry run — re-run with --apply to copy images and rewrite URLs.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
