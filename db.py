"""
Database helper. Connects to Supabase and saves phones + price snapshots.
All scrapers import from here.
"""
import os
import time as _time
from datetime import datetime, timezone
import httpx
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


# Warranty is stored in DAYS (the canonical comparable unit). Sources that quote
# whole months/years are converted with this fixed convention (1 month = 30 days,
# 1 year = 365 days); the UI divides back by 30 for the month display.
MONTH_DAYS = 30
YEAR_DAYS = 365


def months_to_days(months):
    """Convert a whole-month warranty to days (None-safe)."""
    return int(months) * MONTH_DAYS if months is not None else None

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Monthly SEO catalog pass: when INCLUDE_OOS=1, scrapers also save out-of-stock
# variants (in_stock=false, availability=out_of_stock) so model pages exist even
# when nothing is buyable. Off by default — the regular crawler is available-only.
INCLUDE_OOS = os.environ.get("INCLUDE_OOS") == "1"


# Supabase/PostgREST serves over HTTP/2, which caps a single connection at ~20k
# streams (requests) before the server sends GOAWAY and the connection dies. The
# monthly OOS catalog makes far more DB calls than that, so proactively rebuild
# the client onto a fresh connection every few thousand write ops.
_ops_since_reconnect = 0
_OPS_PER_CONNECTION = 6000


def _note_op(n=1):
    global _ops_since_reconnect, supabase
    _ops_since_reconnect += n
    if _ops_since_reconnect >= _OPS_PER_CONNECTION:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        _ops_since_reconnect = 0


# Supabase occasionally drops the HTTP/2 connection mid-request (a graceful
# GOAWAY → httpx.RemoteProtocolError "ConnectionTerminated", or a transient
# network blip). A single such drop used to crash a whole scraper (and, in the
# GitHub job, skip every later store). _exec retries the operation on a freshly
# rebuilt client so a transient drop self-heals instead of aborting the run.
_TRANSIENT = (
    httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError,
    httpx.WriteError, httpx.PoolTimeout, httpx.ReadTimeout, httpx.ConnectTimeout,
)


def _exec(build, tries=4):
    """Run a query-builder lambda, retrying transient connection drops on a new
    client. `build` MUST construct and execute the query referencing the global
    `supabase` (not a captured client) so each retry uses the rebuilt one."""
    global supabase
    delay = 1
    for attempt in range(tries):
        try:
            return build()
        except _TRANSIENT:
            if attempt == tries - 1:
                raise
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            _time.sleep(delay)
            delay = min(delay * 2, 8)


def better_offer(new_availability, new_price, cur):
    """Pick the offer to keep for a (variant_key, condition). in_stock beats
    out_of_stock; within the same availability the lower price wins. `cur` is the
    current offer dict ({availability, price}) or None."""
    if cur is None:
        return True
    new_in = new_availability == "in_stock"
    cur_in = cur.get("availability") == "in_stock"
    if new_in != cur_in:
        return new_in
    return new_price < cur["price"]


def save_phone(site, name, url, image_url, model, storage, ram, variant_key, in_stock=True):
    """
    Insert a phone offer if new for this (site, name), else return existing id.
    'name' stays the raw site title; model/storage/ram/variant_key are normalized.
    in_stock=False is used by the monthly OOS catalog pass.
    """
    existing = _exec(lambda: (
        supabase.table("phones")
        .select("id")
        .eq("site", site)
        .eq("name", name)
        .execute()
    ))
    now = _utcnow_iso()
    if existing.data:
        pid = existing.data[0]["id"]
        # Stamp last_seen_at for the OOS sweep; in_stock reflects this sighting.
        _exec(lambda: supabase.table("phones").update({
            "url": url, "image_url": image_url, "model": model,
            "storage": storage, "ram": ram, "variant_key": variant_key,
            "last_seen_at": now, "in_stock": in_stock,
        }).eq("id", pid).execute())
        _note_op(2)
        return pid

    inserted = _exec(lambda: supabase.table("phones").insert({
        "site": site, "name": name, "url": url, "image_url": image_url,
        "model": model, "storage": storage, "ram": ram, "variant_key": variant_key,
        "last_seen_at": now, "in_stock": in_stock,
    }).execute())
    _note_op(2)
    return inserted.data[0]["id"]


def save_price(phone_id, price, availability="in_stock", condition="Premium Renewed",
               rating=None, review_count=None, warranty_days=None, url=None,
               warranty_label=None):
    """Append a price snapshot (one per condition). Never overwrites; history accrues.

    warranty_days: warranty duration in DAYS (the canonical, comparable unit —
      a month is stored as 30 days, a year as 365). The UI converts back to
      months for display and keeps days only when below a month. See CLAUDE.md.
    warranty_label: text override for warranties with no fixed seller-backed
      duration — "Brand Warranty" (manufacturer/Apple/Samsung). Leave null when
      warranty_days says it all."""
    _exec(lambda: supabase.table("prices").insert({
        "phone_id": phone_id, "price": price, "availability": availability,
        "condition": condition, "rating": rating, "review_count": review_count,
        "warranty_days": warranty_days, "warranty_label": warranty_label,
        "url": url,
    }).execute())
    _note_op(1)



def mark_site_oos(site):
    """Delete today's prices for this site before scraping.
    Called at the start of each scraper run so stale in_stock prices are cleared.
    Only phones found in the current scrape will have prices — others show as unavailable.
    Keeps price history older than today intact for trend tracking.
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Get all phone IDs for this site
    phones = _exec(lambda: supabase.table("phones").select("id").eq("site", site).execute())
    phone_ids = [p["id"] for p in (phones.data or [])]
    if not phone_ids:
        return 0
    # One delete per 100 phones (was one-per-phone — thousands of requests that
    # helped exhaust the HTTP/2 connection on big OOS runs).
    for i in range(0, len(phone_ids), 100):
        batch_ids = phone_ids[i:i+100]
        _exec(lambda b=batch_ids: supabase.table("prices").delete().in_("phone_id", b).gte("scraped_at", today).execute())
        _note_op(1)
    return len(phone_ids)


def _record_run(site, seen, total, complete):
    """Log this run's yield to scrape_runs for the self-healing triage (silent
    yield-drop detection). Best-effort: never block a scrape if the table is
    absent (pre-migration) or the write blips."""
    try:
        _exec(lambda: supabase.table("scrape_runs").insert({
            "site": site, "seen_count": seen, "total_count": total, "run_complete": complete,
        }).execute())
    except Exception:
        pass


def mark_unseen_out_of_stock(site, run_started_at, min_seen_ratio=0.5, run_complete=None):
    """Flag this site's phones that were NOT seen during the run as out of stock.

    Call at the END of a scraper, passing the timestamp captured at the START of
    the run. save_phone() stamps last_seen_at=now on every phone it sees, so any
    phone with last_seen_at < run_started_at (or null) wasn't in this run.

    Guard: a crashed/blocked run must not wipe a whole store to out of stock.
    Preferred signal — pass `run_complete` (the scraper's own health check, e.g.
    "parsed variant data for >=X% of listed products"): True runs the sweep, False
    skips it. When run_complete is None, fall back to the legacy `min_seen_ratio`
    count guard (fine for stores where a run reliably re-sees most rows).
    """
    total = (_exec(lambda: supabase.table("phones").select("id", count="exact")
             .eq("site", site).execute()).count or 0)
    if total == 0:
        return 0
    seen = (_exec(lambda: supabase.table("phones").select("id", count="exact")
            .eq("site", site).gte("last_seen_at", run_started_at).execute()).count or 0)
    if run_complete is None:
        if seen < max(1, int(total * min_seen_ratio)):
            print(f"  [oos] {site}: only {seen}/{total} phones seen this run — skipping OOS sweep (guard)")
            _record_run(site, seen, total, False)
            return 0
    elif not run_complete:
        print(f"  [oos] {site}: run reported incomplete (scraper health) — skipping OOS sweep")
        _record_run(site, seen, total, False)
        return 0
    # Not seen this run -> out of stock (older last_seen_at, or never stamped).
    _exec(lambda: supabase.table("phones").update({"in_stock": False}).eq("site", site).lt("last_seen_at", run_started_at).execute())
    _exec(lambda: supabase.table("phones").update({"in_stock": False}).eq("site", site).is_("last_seen_at", "null").execute())
    still_in = (_exec(lambda: supabase.table("phones").select("id", count="exact")
                .eq("site", site).eq("in_stock", True).execute()).count or 0)
    n = total - still_in
    # Per-condition availability: a phone can be in stock while one of its grades
    # is sold out. Mark grades not refreshed this run as out_of_stock too.
    cond_oos = _mark_disappeared_conditions_oos(site, run_started_at)
    print(f"  [oos] {site}: {seen}/{total} seen, {n} phones OOS, {cond_oos} conditions OOS")
    _record_run(site, seen, total, True)
    return n


def _mark_disappeared_conditions_oos(site, run_started_at):
    """For ALL the site's phones, mark any (phone, condition) whose latest price is
    in_stock but was NOT refreshed this run as out of stock (append an out_of_stock
    snapshot copying the last price/url). Covers both a SEEN phone whose grade sold
    out AND a fully-UNSEEN phone (none of its grades refreshed) — so
    latest_prices.availability stays consistent with the phones.in_stock flag rather
    than showing a stale in_stock for a sold-out phone. Idempotent: skips conditions
    whose latest row is already OOS.
    """
    seen = (_exec(lambda: supabase.table("phones").select("id")
            .eq("site", site).execute()).data or [])
    seen_ids = [p["id"] for p in seen]
    inserted = 0
    for i in range(0, len(seen_ids), 50):
        batch = seen_ids[i:i + 50]
        # (phone, condition) combos that got a fresh row this run (server-side
        # timestamp compare avoids string-format pitfalls).
        fresh = {(r["phone_id"], r["condition"]) for r in
                 (_exec(lambda b=batch: supabase.table("prices").select("phone_id, condition")
                  .in_("phone_id", b).gte("scraped_at", run_started_at).execute()).data or [])}
        all_rows = (_exec(lambda b=batch: supabase.table("prices")
                    .select("phone_id, condition, price, availability, scraped_at, url")
                    .in_("phone_id", b).execute()).data or [])
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
            _exec(lambda r=r: supabase.table("prices").insert({
                "phone_id": r["phone_id"], "price": r["price"],
                "availability": "out_of_stock", "condition": r["condition"],
                "url": r.get("url"),
            }).execute())
            inserted += 1
    return inserted


# ---------- Image hosting on Cloudflare R2 (first sighting only) ----------
# R2 has zero egress fees, so serving images from it doesn't burn bandwidth the
# way the Supabase Storage CDN did. Config via env; if unset, ensure_image falls
# back to the store's source URL so local/unconfigured runs still work.
import requests as _requests

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.environ.get("R2_BUCKET", "phone-images")
R2_PUBLIC_BASE_URL = (os.environ.get("R2_PUBLIC_BASE_URL") or "").rstrip("/")

_r2_client = None


def _r2():
    """Lazily build an S3 client pointed at R2, or None if not fully configured."""
    global _r2_client
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_PUBLIC_BASE_URL):
        return None
    if _r2_client is None:
        import boto3
        from botocore.config import Config
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _r2_client


def r2_public_url(dest_path):
    return f"{R2_PUBLIC_BASE_URL}/{dest_path}"


def ensure_image(source_url, dest_path):
    """Fallback store image, fetched ONCE per device (re-enabled).

    Every scraper calls this with the product's store image and a
    {site}/{variant_key}.jpg destination; the returned URL goes into
    phones.image_url, which the offers view uses as the LAST-RESORT card image
    (after the Beebom/admin specs.image_url and the GSMArena
    specs.image_fallback) so no in-stock card is ever blank.

    "Once per device": if the device already has a hosted store image (its key
    already exists on R2) it is returned WITHOUT downloading anything — one
    paginated LIST per store prefix per run makes that check free per phone.
    Only devices with no hosted image yet are downloaded + uploaded
    (host_image). Failures aren't cached, so they retry next run. Without R2
    creds the raw store URL is returned so runs still work.
    """
    if not source_url:
        return None
    client = _r2()
    if client is None:
        return source_url  # not configured — use the store's image directly
    prefix = dest_path.split("/", 1)[0] + "/"
    try:
        existing = _r2_prefix_keys(client, prefix)
    except Exception:
        existing = None  # listing failed — host_image's own HEAD check covers us
    if existing is not None and dest_path in existing:
        return r2_public_url(dest_path)
    hosted = host_image(source_url, dest_path)
    if existing is not None and hosted == r2_public_url(dest_path):
        existing.add(dest_path)  # hosted OK — skip for the rest of the run
    return hosted


# Existing R2 keys per top-level prefix ("cashify/"), listed lazily ONCE per run
# so ensure_image's once-per-device check costs one LIST per store rather than a
# HEAD or download per phone.
_r2_seen_keys = {}


def _r2_prefix_keys(client, prefix):
    keys = _r2_seen_keys.get(prefix)
    if keys is None:
        keys = set()
        token = None
        while True:
            kw = {"Bucket": R2_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                keys.add(o["Key"])
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        _r2_seen_keys[prefix] = keys
    return keys


def host_image(source_url, dest_path):
    """Host source_url on Cloudflare R2 at dest_path (first sighting only) and
    return its public URL. If R2 isn't configured, return the source URL so runs
    still work without creds. Returns None only when there's nothing usable.
    Used by the GSMArena enrichment and admin image uploads (NOT the scrapers)."""
    if not source_url:
        return None
    client = _r2()
    if client is None:
        return source_url  # not configured — use the store's image directly

    # 1) First-sighting check: skip the download if the object already exists.
    try:
        client.head_object(Bucket=R2_BUCKET, Key=dest_path)
        return r2_public_url(dest_path)
    except Exception:
        pass  # not present (or head failed) — proceed to upload

    # 2) Download the source image.
    try:
        r = _requests.get(source_url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        r.raise_for_status()
        data = r.content
        content_type = r.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        print(f"    image download failed: {e}")
        return source_url  # keep the source URL as a fallback

    # 3) Upload to R2. Long, immutable cache — the key is content-stable per
    # device/model, so a 1-year cache is safe and fixes the "efficient cache
    # lifetimes" Lighthouse audit (Cloudflare's image transform honours the
    # origin Cache-Control). Re-runs overwrite the same key when an image changes.
    try:
        client.put_object(Bucket=R2_BUCKET, Key=dest_path, Body=data,
                          ContentType=content_type,
                          CacheControl="public, max-age=31536000, immutable")
    except Exception as e:
        print(f"    image upload failed: {e}")
        return source_url
    return r2_public_url(dest_path)