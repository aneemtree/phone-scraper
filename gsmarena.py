"""
GSMArena specs + canonical-image enrichment.

GOAL: every phone card (identified by variant_key — model+storage, NOT grade) gets
a clean spec sheet and a canonical product image from GSMArena. Runs INCREMENTALLY:
only variant_keys present in `phones` but missing from `specs` are processed, and a
key is fetched ONCE (a match — or a recorded `not_found` — is never re-fetched).

WHY THIS DESIGN (no per-model search, no rate-limiting):
GSMArena has no API and rate-limits search, BUT its autocomplete downloads the
ENTIRE device database as one static JSON (/quicksearch-<n>.jpg):
  data[0] = {maker_id: maker_name}
  data[1] = [[maker_id, device_id, model_name, keywords, image_file, short_name], ...]
We fetch that once and match every model locally. Only the matched devices' spec
PAGES are fetched over the network (one GET each), parsed via stable data-spec
attributes; the image comes from the page's bigpic URL (R2-hosted as primary).

MATCHING (auto, brand-aware, conservative): tokens of our model must be a subset of
the device's name+keyword tokens (so aliases catch "Flip 6"↔"Flip6", "+"↔"Plus",
"(2a)"↔"2a", and "iPhone Air"↔"iPhone 17 Air"); among those, the device with the
FEWEST extra name tokens wins. No candidate within tolerance → status 'not_found'
(never a wrong guess). The card image becomes primary via the offers view
(coalesce(specs.image_url, phones.image_url)).

Run:  python3 gsmarena.py            # enrich all variant_keys missing specs
      python3 gsmarena.py --dry       # print proposed matches (+sample specs), NO writes
      python3 gsmarena.py --limit N   # cap how many keys to process (testing)
"""
import gzip
import json
import os
import random
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request

GSM_BASE = "https://www.gsmarena.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/json,*/*",
           "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip"}
DELAY = float(os.environ.get("GSM_DELAY") or (8.0 if "--slow" in sys.argv else 2.5))
MATCH_MAX_EXTRA = 2

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# Tokens that don't help identify a model (connectivity/marketing/packaging noise).
_STOP = set("5g 4g 3g 2g lte volte nfc android smartphone phone with dual sim "
            "esim e-sim physical new moto".split())
# "moto" is dropped: clean_model adds the Motorola sub-brand ("Motorola Moto Razr
# 50 Ultra"), but GSMArena lists Razr without it ("Motorola Razr 50 Ultra"); the
# redundant token otherwise breaks the subset match. Moto G/E still match (their
# "moto" becomes a tolerated extra device token).


class _Resp:
    def __init__(self, status_code, text, headers):
        self.status_code = status_code
        self.text = text
        self.headers = headers

    def json(self):
        return json.loads(self.text)


def _get(url, tries=4):
    delay = 30.0
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            resp = urllib.request.urlopen(req, timeout=45, context=_CTX)
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return _Resp(200, raw.decode("utf-8", "replace"), resp.headers)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = delay
                ra = e.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(wait, float(ra))
                time.sleep(min(wait, 300) + random.uniform(0, 3))
                delay = min(delay * 2, 300)
                continue
            return _Resp(e.code, "", e.headers)
        except Exception:
            time.sleep(delay + random.uniform(0, 3))
            delay = min(delay * 2, 300)
    return _Resp(0, "", {})


# ----------------------------------------------------------------------------- match
def _toks(s):
    """Identity tokens: lowercase, '+'→'plus', drop ()/punct and noise words."""
    s = (s or "").lower().replace("+", " plus ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t and t not in _STOP]


def device_name_tokens(d):
    return set(_toks(d["full"]))


def device_alias_tokens(d):
    return set(_toks(d["full"] + " " + d["keywords"] + " " + d["short"]))


def best_match(model, devices):
    """Return (device, score) or (None, 0.0). Brand-aware subset match; fewest
    extra name tokens wins; reject if too loose."""
    ours = _toks(model)
    if not ours:
        return None, 0.0
    ours_set = set(ours)
    brand = ours[0]
    best, best_extra = None, 99
    for d in devices:
        alias = device_alias_tokens(d)
        if brand not in alias:            # must be the same brand
            continue
        if not ours_set <= alias:         # all our tokens must be present
            continue
        extra = len(device_name_tokens(d) - ours_set)
        if extra < best_extra:
            best, best_extra = d, extra
            if extra == 0:
                break
    if best is None or best_extra > MATCH_MAX_EXTRA:
        return None, 0.0
    return best, round(1.0 / (1 + best_extra), 3)


# ------------------------------------------------------------------------- quicksearch
# The device DB rarely changes, and homepage/quicksearch are the most throttle-prone
# calls — so cache the parsed list locally and reuse it for a week. A manually saved
# quicksearch JSON can be supplied via GSM_DEVICES_FILE to bypass the network.
_CACHE = os.environ.get("GSM_DEVICES_FILE") or os.path.join(
    tempfile.gettempdir(), "gsmarena_devices.json")
_CACHE_TTL = 7 * 86400


def _parse_quicksearch(data):
    makers, rows = data[0], data[1]
    out = []
    for e in rows:
        maker_id, dev_id, model_name, keywords, image, short = (list(e) + [None] * 6)[:6]
        maker = makers.get(str(maker_id), "")
        out.append({
            "id": dev_id, "maker": maker, "model_name": model_name or "",
            "keywords": keywords or "", "image": image or "", "short": short or "",
            "full": f"{maker} {model_name}".strip(),
        })
    return out


def load_devices():
    """GSMArena's full device DB (one static JSON) → list of device dicts. Cached
    locally for a week; falls back to a stale cache if GSMArena is throttling."""
    if os.path.exists(_CACHE) and time.time() - os.path.getmtime(_CACHE) < _CACHE_TTL:
        with open(_CACHE) as f:
            return _parse_quicksearch(json.load(f))
    try:
        home = _get(GSM_BASE + "/").text
        m = re.search(r"/quicksearch-(\d+)\.jpg", home)
        if not m:
            raise RuntimeError("no quicksearch link on homepage (likely a Cloudflare "
                               "challenge — GSMArena is throttling this IP)")
        data = _get(GSM_BASE + m.group(0)).json()
        with open(_CACHE, "w") as f:
            json.dump(data, f)
        return _parse_quicksearch(data)
    except Exception as e:
        if os.path.exists(_CACHE):       # better stale than nothing
            print(f"  device-DB fetch failed ({e}); using cached copy at {_CACHE}")
            with open(_CACHE) as f:
                return _parse_quicksearch(json.load(f))
        raise RuntimeError(
            f"{e}\nCould not load the GSMArena device DB and no cache exists. "
            f"GSMArena is likely throttling this IP — wait ~15 min and retry, or save "
            f"the quicksearch JSON from a browser and point GSM_DEVICES_FILE at it.")


def device_page_url(d):
    slug = re.sub(r"\.\w+$", "", d["image"]).replace("-", "_")
    return f"{GSM_BASE}/{slug}-{d['id']}.php"


# ------------------------------------------------------------------------- spec page
def parse_specs(html):
    """Spec sheet from stable data-spec attributes → {key: text}. Drops inline-JS
    noise that occasionally trails a value cell."""
    out = {}
    for k, v in re.findall(r'data-spec="([^"]+)"[^>]*>(.*?)</td>', html, re.S):
        v = re.split(r"\bvar\s+\w+\s*=", v)[0]   # cut trailing inline script
        v = re.sub(r"<[^>]+>", " ", v)
        v = re.sub(r"\s+", " ", v).strip()
        if v:
            out[k] = v
    return out


def page_image(html):
    m = re.search(r"https://fdn[0-9]*\.gsmarena\.com/vv/bigpic/[^\"'\s>]+", html)
    return m.group(0) if m else None


def fetch_device(d):
    """Fetch the device page → (specs_dict, image_url, url, ok). ok is False when
    the page is missing or a throttle/challenge page (HTTP 200 but no data-spec) —
    so the caller can back off and NOT persist an empty sheet as 'ok'. The image
    falls back to the quicksearch image filename."""
    url = device_page_url(d)
    r = _get(url)
    img = (f"https://fdn2.gsmarena.com/vv/bigpic/{d['image']}" if d["image"] else None)
    if not r or r.status_code != 200 or "data-spec" not in r.text:
        return None, img, url, False        # blocked / challenge / missing
    specs = parse_specs(r.text)
    return specs, (page_image(r.text) or img), url, bool(specs)


# ------------------------------------------------------------------------------ DB I/O
def _fetch_all(table, columns):
    """Page through every row (PostgREST caps a select at 1000 by default)."""
    from db import supabase
    out, start, step = [], 0, 1000
    while True:
        chunk = (supabase.table(table).select(columns)
                 .range(start, start + step - 1).execute().data or [])
        out.extend(chunk)
        if len(chunk) < step:
            return out
        start += step


def _targets(images_only=False):
    """One (key, model) per distinct phone MODEL that still needs work, so specs
    and images are fetched once per model and shared across all storage variants.
    key = coalesce(canonical_key, variant_key) (used for the row id / image path).
    A model is done when any specs row for it qualifies:
      images_only -> has an image_url; specs -> has specs OR status='not_found'."""
    phones = _fetch_all("phones", "variant_key,canonical_key,model")
    status = {}
    try:
        rows = _fetch_all("specs", "model,status,specs,image_url")
        for r in rows:
            m = (r.get("model") or "").lower()
            st = status.setdefault(m, {"specs": False, "image": False, "nf": False})
            if r.get("specs"):
                st["specs"] = True
            if r.get("image_url"):
                st["image"] = True
            if r.get("status") == "not_found":
                st["nf"] = True
    except Exception as e:
        print(f"  (specs table not ready: {e}; assuming none enriched)")
    todo, seen = [], set()
    for p in phones:
        model = p.get("model") or ""
        key = p.get("canonical_key") or p.get("variant_key")
        if not model or not key or model.lower() in seen:
            continue
        st = status.get(model.lower())
        done = (st and st["image"]) if images_only else (st and (st["specs"] or st["nf"]))
        if done:
            continue
        seen.add(model.lower())
        todo.append((key, model))
    return todo


def upsert_specs(model, fields):
    """Merge fields into the per-MODEL specs row (one row per model, shared across
    storage variants and across enrichers). Updates the existing row(s) for the
    model if present, else inserts one keyed by the storage-less model slug. This
    lets GSMArena (specs) and Beebom (image) write to the same row without
    clobbering each other."""
    from db import supabase, _exec, _note_op
    from normalize import make_variant_key
    existing = _exec(lambda: supabase.table("specs").select("variant_key")
                     .eq("model", model).limit(1).execute()).data
    if existing:
        _exec(lambda: supabase.table("specs").update(fields).eq("model", model).execute())
    else:
        row = {"variant_key": make_variant_key(model, None), "model": model, **fields}
        _exec(lambda: supabase.table("specs").insert(row).execute())
    _note_op(1)


def save_specs(key, model, device, image_fallback, specs, score, status):
    fields = {
        "gsm_id": device["id"] if device else None,
        "gsm_url": device_page_url(device) if device else None,
        "gsm_name": device["full"] if device else None,
        "specs": specs or None, "match_score": score, "status": status,
    }
    if image_fallback:
        fields["image_fallback"] = image_fallback   # GSMArena image (fallback only)
    upsert_specs(model, fields)


def set_image(model, source_url):
    """Admin: host an image as the canonical image for a MODEL (shared across all
    its storage variants), merged into its specs row. Pass the exact model name
    shown in missing_images."""
    from db import host_image
    from normalize import make_variant_key
    hosted = host_image(source_url, f"admin/{make_variant_key(model, None)}.jpg") or source_url
    upsert_specs(model, {"image_url": hosted, "image_source": "admin"})
    print(f"set admin image for {model}: {hosted}")
    return hosted


# -------------------------------------------------------------------------------- runs
def enrich(limit=None):
    from db import host_image
    from normalize import make_variant_key
    print("Loading GSMArena device DB...")
    devices = load_devices()
    print(f"  {len(devices)} devices loaded.")
    todo = _targets()
    keys = todo[:limit] if limit else todo
    print(f"{len(keys)} models missing specs.\n")

    matched = notfound = blocked = 0
    fails = 0
    for key, model in keys:
        device, score = best_match(model, devices)
        if not device:
            save_specs(key, model, None, None, None, 0.0, "not_found")
            notfound += 1
            print(f"  NOT FOUND  {model:32} [{key}]")
            continue
        specs, img, _, ok = fetch_device(device)
        if not ok:
            fails += 1
            blocked += 1
            print(f"  BLOCKED    {model:30} -> {device['full']} (no specs; retries next run)")
            if fails >= 5:
                print("\nGSMArena is throttling (5 blocked in a row). Stopping - "
                      "re-run later to resume from where it left off.")
                break
            time.sleep(30)
            continue
        fails = 0
        hosted = host_image(img, f"specs/{make_variant_key(model, None)}.jpg") if img else None
        save_specs(key, model, device, hosted or img, specs, score, "ok")
        matched += 1
        print(f"  ok({score})  {model:30} -> {device['full']:32} "
              f"({len(specs or {})} specs) [{key}]")
        time.sleep(DELAY + random.uniform(0, DELAY))

    print(f"\nDone. matched={matched} not_found={notfound} blocked={blocked}")


def dry_run(limit=None):
    print("Loading GSMArena device DB...")
    devices = load_devices()
    print(f"  {len(devices)} devices loaded.")
    todo = _targets(images_only=False)
    keys = todo[:limit] if limit else todo
    print(f"{len(keys)} models missing specs.\n")
    matched = notfound = 0
    sample_done = 0
    for key, model in sorted(keys, key=lambda kv: kv[1]):
        device, score = best_match(model, devices)
        if not device:
            notfound += 1
            print(f"  NOT FOUND   {model}")
            continue
        matched += 1
        print(f"  {score:<5} {model:30} -> {device['full']}")
        if sample_done < 3:                  # show a real spec fetch for a few
            specs, img, url, ok = fetch_device(device)
            print(f"        img: {img}")
            for k in ("displaysize", "chipset", "battery", "batdescription1",
                      "os", "internalmemory"):
                if specs and k in specs:
                    print(f"        {k}: {specs[k][:60]}")
            sample_done += 1
            time.sleep(DELAY)
    print(f"\nWOULD match={matched} not_found={notfound} "
          f"(rate={matched/(matched+notfound)*100:.0f}%)" if (matched+notfound) else "")


if __name__ == "__main__":
    if "--set-image" in sys.argv:
        i = sys.argv.index("--set-image")
        set_image(sys.argv[i + 1], sys.argv[i + 2])
        sys.exit(0)
    lim = None
    if "--limit" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--dry" in sys.argv:
        dry_run(lim)
    else:
        from obs import init_sentry, log_error
        init_sentry("gsmarena")
        try:
            enrich(lim)
        except Exception as e:
            log_error(e, site="gsmarena", phase="enrich")
            raise
