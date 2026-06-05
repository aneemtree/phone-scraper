"""
Delete orphaned images from Cloudflare R2.

The canonical image now lives under `img/` (Beebom) and `admin/` (manual uploads).
Everything else in the bucket is stale: the old GSMArena renders under `specs/` and
the legacy per-store images under `{site}/`. This removes them.

SAFETY: dry-run by default (lists what would be deleted). Pass --delete to actually
remove. Run this ONLY after the Beebom backfill is complete AND old GSMArena image
URLs have been cleared from the specs table, e.g.:

    update specs set image_url = null, image_source = null
     where image_source is distinct from 'beebom'
       and image_source is distinct from 'admin';

    python3 cleanup_r2_images.py            # dry run: show what would go
    python3 cleanup_r2_images.py --delete    # actually delete
"""
import sys

KEEP_PREFIXES = ("img/", "admin/")


def _iter_keys(client, bucket):
    token = None
    while True:
        kw = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            yield o["Key"]
        if not resp.get("IsTruncated"):
            return
        token = resp.get("NextContinuationToken")


def main(delete=False):
    from db import _r2, R2_BUCKET
    client = _r2()
    if client is None:
        print("R2 not configured (R2_* env not set).")
        return
    todelete = [k for k in _iter_keys(client, R2_BUCKET)
                if not k.startswith(KEEP_PREFIXES)]
    print(f"{len(todelete)} objects outside img/ and admin/ (sample):")
    for k in todelete[:25]:
        print("   ", k)
    if not delete:
        print("\nDry run. Re-run with --delete to remove these.")
        return
    removed = 0
    for i in range(0, len(todelete), 1000):
        batch = [{"Key": k} for k in todelete[i:i + 1000]]
        client.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": batch})
        removed += len(batch)
        print(f"  deleted {removed}/{len(todelete)}")
    print(f"\nDone. Deleted {removed} objects.")


if __name__ == "__main__":
    main(delete="--delete" in sys.argv)
