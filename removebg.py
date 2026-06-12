"""
Background removal for hosted phone images (FREE, self-hosted — no paid API).

Uses `rembg` (U2-Net / ISNet ONNX models, CPU) to cut the background out of the
phone product images already on R2 and write a transparent PNG to a SEPARATE
`nobg/<original-key>.png` prefix. Originals are left untouched and nothing in the
DB or the web UI changes — this is for REVIEW first. Once the cutouts look good
we can flip the offers view / image helper to prefer the `nobg/` version.

Source prefixes ("all images"): every top-level R2 prefix except the ones that
aren't phone product shots (logos/, blog/, nobg/ itself).

Run (via removebg.yml workflow_dispatch, or locally with R2_* env set):
  python3 removebg.py --sample 12      # process ~12 random images, print URLs
  python3 removebg.py --all            # process everything, skip already-done
  REMBG_MODEL=u2netp python3 removebg.py --sample 12   # faster/smaller model

Env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
     R2_PUBLIC_BASE_URL (same as the scrapers); REMBG_MODEL (optional, default
     isnet-general-use — best for product cutouts; u2netp is smaller/faster).
"""
import os
import random
import sys

import db  # reuse the scrapers' R2 client + helpers

IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
SKIP_PREFIXES = ("logos/", "blog/", "nobg/")  # not phone product shots
MODEL = os.environ.get("REMBG_MODEL", "isnet-general-use")

_session = None


def _rembg_session():
    global _session
    if _session is None:
        from rembg import new_session
        _session = new_session(MODEL)
    return _session


def list_source_keys(client):
    """All phone-image object keys across every top-level prefix except the
    skip list. Walks the bucket once."""
    keys = []
    token = None
    while True:
        kw = {"Bucket": db.R2_BUCKET, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            k = o["Key"]
            if k.startswith(SKIP_PREFIXES):
                continue
            if k.lower().endswith(IMG_EXTS):
                keys.append(k)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def dest_key(src_key):
    """nobg/<src path with extension swapped to .png>."""
    base = src_key.rsplit(".", 1)[0]
    return f"nobg/{base}.png"


def process_one(client, src_key, overwrite=False):
    """Download src image from R2, remove background, upload transparent PNG.
    Returns (status, public_url): status in done|skip|empty|error."""
    dst = dest_key(src_key)
    if not overwrite:
        try:
            client.head_object(Bucket=db.R2_BUCKET, Key=dst)
            return "skip", db.r2_public_url(dst)
        except Exception:
            pass
    try:
        data = client.get_object(Bucket=db.R2_BUCKET, Key=src_key)["Body"].read()
        if not data:
            return "empty", None
        from rembg import remove
        out = remove(data, session=_rembg_session())  # PNG bytes (RGBA)
        if not out:
            return "empty", None
        client.put_object(Bucket=db.R2_BUCKET, Key=dst, Body=out, ContentType="image/png")
        return "done", db.r2_public_url(dst)
    except Exception as e:
        print(f"  ERROR {src_key}: {e}")
        return "error", None


def run(sample=None, overwrite=False):
    client = db._r2()
    if client is None:
        print("R2 not configured (need R2_* env). Aborting.")
        return
    keys = list_source_keys(client)
    print(f"{len(keys)} source image(s) found (model={MODEL})")
    if sample:
        random.seed(42)
        keys = random.sample(keys, min(sample, len(keys)))
        print(f"processing a sample of {len(keys)}")

    counts = {"done": 0, "skip": 0, "empty": 0, "error": 0}
    urls = []
    for i, k in enumerate(keys, 1):
        status, url = process_one(client, k, overwrite=overwrite)
        counts[status] += 1
        if status == "done" and url:
            urls.append(url)
        if i % 100 == 0:
            print(f"  {i}/{len(keys)} {counts}")
    print(f"done: {counts}")
    if sample and urls:
        print("\nReview these transparent cutouts:")
        for u in urls:
            print(" ", u)


if __name__ == "__main__":
    args = sys.argv[1:]
    overwrite = "--overwrite" in args
    sample = None
    if "--sample" in args:
        i = args.index("--sample")
        sample = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 12
    elif "--all" not in args:
        sample = 12  # default: a small sample, never the whole bucket by accident
    run(sample=sample, overwrite=overwrite)
