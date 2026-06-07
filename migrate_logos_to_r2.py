"""
One-off migration: move store logos from Supabase Storage to Cloudflare R2.

Why: logos in the `logos` Supabase bucket aren't on our Cloudflare edge and
aren't resized for the ~22px they render at. Hosting them on R2 lets the web app
serve them through Cloudflare image transformations (imageUrl(): format=auto,
width=…) — small, WebP/AVIF, edge-cached. Stores' logo_url is updated to the R2
URL so the frontend (which rewrites R2 hosts onto phnfy.com/cdn-cgi/image) picks
it up automatically. SVG logos are copied too but the frontend serves them
untransformed (Cloudflare's resizer doesn't process vector).

Re-runnable: host_image() skips objects already on R2 (head_object), and rows
already pointing at R2 are left alone.

Requires the same env as the image pipeline: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL, plus SUPABASE_URL/_SERVICE_KEY.

Usage:
  python3 migrate_logos_to_r2.py --dry   # show what would move, no writes
  python3 migrate_logos_to_r2.py         # migrate + update stores.logo_url
"""
import sys
from db import supabase, host_image, R2_PUBLIC_BASE_URL

DRY = "--dry" in sys.argv


def main():
    if not R2_PUBLIC_BASE_URL:
        print("R2 is not configured (set R2_* env vars). Aborting.")
        return

    rows = (supabase.table("stores")
            .select("site, logo_url")
            .not_.is_("logo_url", "null")
            .execute().data) or []

    moved = skipped = failed = 0
    for r in rows:
        site = r["site"]
        url = (r.get("logo_url") or "").strip()
        if not url or R2_PUBLIC_BASE_URL in url:
            skipped += 1
            continue
        if "/storage/v1/object/public/logos/" not in url:
            print(f"  skip {site}: not a Supabase logos URL ({url})")
            skipped += 1
            continue

        fname = url.split("?")[0].rstrip("/").split("/")[-1]
        dest = f"logos/{fname}"
        print(f"  {site}: {fname} -> R2 {dest}")
        if DRY:
            continue

        new_url = host_image(url, dest)  # downloads from Supabase, uploads to R2
        if not new_url or R2_PUBLIC_BASE_URL not in new_url:
            print(f"    FAILED — kept {url}")
            failed += 1
            continue
        supabase.table("stores").update({"logo_url": new_url}).eq("site", site).execute()
        print(f"    -> {new_url}")
        moved += 1

    print(f"\nDone. moved={moved} skipped={skipped} failed={failed}{' (dry run)' if DRY else ''}.")


if __name__ == "__main__":
    main()
