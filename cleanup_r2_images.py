"""
Delete orphaned images from Cloudflare R2.

Kept prefixes: `img/` (Beebom primary), `specs/` (GSMArena fallback), `admin/`
(manual uploads), `logos/` (store logos), and the per-store `{site}/` prefixes —
store images are ACTIVE again (ensure_image hosts each device's store image once
as the last-resort card fallback), so they must NOT be deleted. Only keys outside
all of these (renamed/retired stores, strays) are candidates.

SAFETY: dry-run by default (lists what would be deleted). Pass --delete to remove.

    python3 cleanup_r2_images.py            # dry run: show what would go
    python3 cleanup_r2_images.py --delete    # actually delete
"""
import sys

# Active scraper sites (keep in sync with the scrapers' SITE constants).
SITES = (
    "budli", "cashify", "cellbuddy", "controlz", "easyphones", "gadgetrebirth",
    "grest", "itradeit", "maplestore", "mobilegoo", "oldsold", "ovantica",
    "refit", "sahivalue", "tetro", "thephonehub", "xtracover",
)
KEEP_PREFIXES = ("img/", "specs/", "admin/", "logos/") + tuple(f"{s}/" for s in SITES)


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
    print(f"{len(todelete)} objects outside the kept prefixes (sample):")
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
