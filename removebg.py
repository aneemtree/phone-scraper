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

IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
SKIP_PREFIXES = ("logos/", "blog/", "nobg/")  # not phone product shots
MODEL = os.environ.get("REMBG_MODEL", "isnet-general-use")

# R2 config (same env as the scrapers). Self-contained — does NOT import db.py
# so it pulls no scraper deps (httpx/supabase/etc.), only boto3 + rembg.
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.environ.get("R2_BUCKET", "phone-images")
R2_PUBLIC_BASE_URL = (os.environ.get("R2_PUBLIC_BASE_URL") or "").rstrip("/")

_session = None
_client = None


def _r2():
    global _client
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_PUBLIC_BASE_URL):
        return None
    if _client is None:
        import boto3
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
    return _client


def r2_public_url(key):
    return f"{R2_PUBLIC_BASE_URL}/{key}"


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
        kw = {"Bucket": R2_BUCKET, "MaxKeys": 1000}
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


def _downscale(img_bytes, max_dim):
    """Shrink the input so its longest side is <= max_dim before inference.
    BiRefNet at full resolution OOM-kills the 7GB runner; phone cards render
    <=720px anyway, so ~1024 is ample. MAX_DIM env (0 = no downscale)."""
    if max_dim <= 0:
        return img_bytes
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = im.size
    if max(w, h) <= max_dim:
        return img_bytes
    s = max_dim / max(w, h)
    im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _alpha_stats(png_bytes):
    """Alpha histogram buckets, to diagnose cutouts without viewing pixels.
    opaque = subject kept solid; mid = translucent (bad); clear = background."""
    import io
    from PIL import Image
    a = Image.open(io.BytesIO(png_bytes)).convert("RGBA").getchannel("A")
    h = a.histogram()
    tot = sum(h) or 1
    return {
        "clear%": round(100 * h[0] / tot),
        "low%": round(100 * sum(h[1:50]) / tot),
        "mid%": round(100 * sum(h[50:205]) / tot),
        "opaque%": round(100 * sum(h[205:]) / tot),
    }


def _flood_bgmask(img_bytes, tol):
    """Boolean background mask for a UNIFORM-ish backdrop: near the corner
    colour AND connected to the border. Returns (PIL.Image RGB, ndarray mask)."""
    import io
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    arr = np.asarray(im).astype(np.int16)
    h, w, _ = arr.shape
    c = max(8, min(h, w) // 40)
    corners = np.concatenate([
        arr[:c, :c].reshape(-1, 3), arr[:c, -c:].reshape(-1, 3),
        arr[-c:, :c].reshape(-1, 3), arr[-c:, -c:].reshape(-1, 3),
    ])
    bg = np.median(corners, axis=0)
    near_bg = np.abs(arr - bg).max(axis=2) <= tol
    lbl, _n = ndimage.label(near_bg)
    border = set(lbl[0, :]) | set(lbl[-1, :]) | set(lbl[:, 0]) | set(lbl[:, -1])
    border.discard(0)
    return im, np.isin(lbl, list(border))


def _combo_remove(img_bytes, tol):
    """Best-of-both background removal: a pixel is removed ONLY when both the
    ML matting (ISNet, holes filled) and the border-connected flood agree it's
    background. ML catches gradients/decorative backdrops the flood can't;
    the flood + hole-fill protect subject regions the ML wrongly erases
    (screens, reflections, white/black panels). Keep if EITHER says subject."""
    import io
    import numpy as np
    from PIL import Image, ImageFilter
    from scipy import ndimage
    from rembg import remove

    ml_png = remove(img_bytes, session=_rembg_session(), post_process_mask=True)
    ml_alpha = np.array(Image.open(io.BytesIO(ml_png)).convert("RGBA"))[..., 3]
    ml_subject = ndimage.binary_fill_holes(ml_alpha > 30)

    im, flood_bg = _flood_bgmask(img_bytes, tol)
    bg = flood_bg & ~ml_subject          # both must call it background
    alpha = np.where(bg, 0, 255).astype(np.uint8)
    A = Image.fromarray(alpha, "L").filter(ImageFilter.GaussianBlur(0.6))
    out = im.convert("RGBA")
    out.putalpha(A)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def _flood_remove(img_bytes, tol):
    """Remove a UNIFORM (near-white/grey) studio background by flooding inward
    from the borders: a pixel is background only if it's within `tol` of the
    corner colour AND connected to the edge. Interior light areas (screen,
    reflections) are enclosed by the phone body, so the flood never reaches them
    -> the subject stays intact, no holes. No ML model, no OOM. FLOOD_TOL env."""
    import io
    import numpy as np
    from PIL import Image, ImageFilter
    from scipy import ndimage

    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    arr = np.asarray(im).astype(np.int16)
    h, w, _ = arr.shape
    c = max(8, min(h, w) // 40)
    corners = np.concatenate([
        arr[:c, :c].reshape(-1, 3), arr[:c, -c:].reshape(-1, 3),
        arr[-c:, :c].reshape(-1, 3), arr[-c:, -c:].reshape(-1, 3),
    ])
    bg = np.median(corners, axis=0)
    near_bg = np.abs(arr - bg).max(axis=2) <= tol     # colour close to background
    lbl, _n = ndimage.label(near_bg)
    border = set(lbl[0, :]) | set(lbl[-1, :]) | set(lbl[:, 0]) | set(lbl[:, -1])
    border.discard(0)
    bgmask = np.isin(lbl, list(border))               # near-bg AND edge-connected
    alpha = np.where(bgmask, 0, 255).astype(np.uint8)
    A = Image.fromarray(alpha, "L").filter(ImageFilter.GaussianBlur(0.6))  # soft edge
    out = im.convert("RGBA")
    out.putalpha(A)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def _fill_holes(png_bytes):
    """The model erases light/glossy regions INSIDE the phone (screens,
    reflections, white backs) that blend with a light background, punching
    transparent holes in the subject. Fill any transparent region fully
    enclosed by the subject (make it opaque) — rembg keeps the original RGB, so
    this reveals the real screen/reflection pixels. FILL_HOLES env (default on)."""
    if os.environ.get("FILL_HOLES", "1") not in ("1", "true", "True"):
        return png_bytes
    import io
    import numpy as np
    from PIL import Image
    try:
        from scipy import ndimage
    except Exception:
        return png_bytes
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.array(im)
    mask = arr[..., 3] > 30
    holes = ndimage.binary_fill_holes(mask) & ~mask
    if holes.any():
        arr[..., 3][holes] = 255
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def _boost_alpha(png_bytes):
    """ISNet/U2-Net return a SOFT mask, so glossy/dark phones come out partly
    see-through. MULTIPLY the alpha (clip to 255) so the translucent subject
    becomes opaque while fully-transparent background stays 0 and edge
    anti-aliasing is preserved. This does NOT erase faint subject pixels the way
    a hard threshold did. ALPHA_GAIN env (default 3; <=1 = raw mask)."""
    import io
    g = float(os.environ.get("ALPHA_GAIN", "3"))
    if g <= 1:
        return png_bytes
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    a = im.getchannel("A").point(lambda v: min(255, int(v * g)))
    im.putalpha(a)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def process_one(client, src_key, overwrite=False):
    """Download src image from R2, remove background, upload transparent PNG.
    Returns (status, public_url): status in done|skip|empty|error."""
    dst = dest_key(src_key)
    if not overwrite:
        try:
            client.head_object(Bucket=R2_BUCKET, Key=dst)
            return "skip", r2_public_url(dst)
        except Exception:
            pass
    try:
        data = client.get_object(Bucket=R2_BUCKET, Key=src_key)["Body"].read()
        if not data:
            return "empty", None
        data = _downscale(data, int(os.environ.get("MAX_DIM", "1024")))
        method = os.environ.get("METHOD", "combo")
        if method == "combo":
            # Default: remove only where ML matting AND flood agree it's background.
            out = _combo_remove(data, int(os.environ.get("FLOOD_TOL", "72")))
        elif method == "flood":
            # Uniform-background flood removal only (no ML).
            out = _flood_remove(data, int(os.environ.get("FLOOD_TOL", "32")))
        else:
            # ML matting (rembg) — for non-uniform backgrounds.
            from rembg import remove
            out = remove(data, session=_rembg_session(), post_process_mask=True)
            if not out:
                return "empty", None
            out = _fill_holes(out)
            out = _boost_alpha(out)
        if os.environ.get("INSPECT"):
            print(f"  STATS {src_key} ({method}): {_alpha_stats(out)}")
        client.put_object(Bucket=R2_BUCKET, Key=dst, Body=out, ContentType="image/png")
        return "done", r2_public_url(dst)
    except Exception as e:
        print(f"  ERROR {src_key}: {e}")
        return "error", None


def run(sample=None, overwrite=False):
    client = _r2()
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
