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
import re
import sys
import time
import requests

GSM_BASE = "https://www.gsmarena.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/json,*/*",
           "Accept-Language": "en-US,en;q=0.9"}
DELAY = 1.5          # polite gap between spec-page fetches
MATCH_MAX_EXTRA = 2  # reject a match if the device name has >this many extra tokens

_session = requests.Session()
_session.headers.update(HEADERS)

# Tokens that don't help identify a model (connectivity/marketing/packaging noise).
_STOP = set("5g 4g 3g 2g lte volte nfc android smartphone phone with dual sim "
            "esim e-sim physical new".split())


def _get(url, tries=4):
    delay = 1.0
    r = None
    for _ in range(tries):
        r = _session.get(url, timeout=45)
        if r.status_code == 200:
            return r
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(delay)
            delay = min(delay * 2, 16)
            continue
        return r
    return r


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
def load_devices():
    """Fetch GSMArena's full device DB (one static JSON) → list of device dicts."""
    home = _get(GSM_BASE + "/").text
    m = re.search(r"/quicksearch-(\d+)\.jpg", home)
    if not m:
        raise RuntimeError("could not find quicksearch URL on GSMArena homepage")
    data = _get(GSM_BASE + m.group(0)).json()
    makers, rows = data[0], data[1]
    devices = []
    for e in rows:
        maker_id, dev_id, model_name, keywords, image, short = (list(e) + [None] * 6)[:6]
        maker = makers.get(str(maker_id), "")
        devices.append({
            "id": dev_id,
            "maker": maker,
            "model_name": model_name or "",
            "keywords": keywords or "",
            "image": image or "",
            "short": short or "",
            "full": f"{maker} {model_name}".strip(),
        })
    return devices


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
    """Fetch the device page → (specs_dict, image_url). image falls back to the
    quicksearch image filename if the page yields none."""
    url = device_page_url(d)
    r = _get(url)
    if not r or r.status_code != 200:
        return None, None, url
    specs = parse_specs(r.text)
    img = page_image(r.text) or (f"https://fdn2.gsmarena.com/vv/bigpic/{d['image']}"
                                 if d["image"] else None)
    return specs, img, url


# ------------------------------------------------------------------------------ DB I/O
def _missing_keys():
    """Distinct (key, sample model) present in phones but absent from specs.
    key = coalesce(canonical_key, variant_key) to honour the manual-merge fallback."""
    from db import supabase
    phones = supabase.table("phones").select("variant_key,canonical_key,model").execute().data or []
    have = {row["variant_key"] for row in
            (supabase.table("specs").select("variant_key").execute().data or [])}
    todo = {}
    for p in phones:
        key = p.get("canonical_key") or p.get("variant_key")
        if not key or key in have or key in todo:
            continue
        todo[key] = p.get("model") or ""
    return todo


def save_specs(key, model, device, specs, image_url, score, status):
    from db import supabase, _exec, _note_op
    row = {
        "variant_key": key, "model": model,
        "gsm_id": device["id"] if device else None,
        "gsm_url": device_page_url(device) if device else None,
        "gsm_name": device["full"] if device else None,
        "image_url": image_url, "specs": specs or None,
        "match_score": score, "status": status,
    }
    _exec(lambda: supabase.table("specs").upsert(row, on_conflict="variant_key").execute())
    _note_op(1)


# -------------------------------------------------------------------------------- runs
def enrich(limit=None):
    from db import ensure_image
    print("Loading GSMArena device DB...")
    devices = load_devices()
    print(f"  {len(devices)} devices loaded.")
    todo = _missing_keys()
    keys = list(todo.items())
    if limit:
        keys = keys[:limit]
    print(f"{len(keys)} variant_keys missing specs.\n")

    matched = notfound = 0
    for key, model in keys:
        device, score = best_match(model, devices)
        if not device:
            save_specs(key, model, None, None, None, 0.0, "not_found")
            notfound += 1
            print(f"  NOT FOUND  {model:32} [{key}]")
            continue
        specs, img, _ = fetch_device(device)
        hosted = ensure_image(img, f"specs/{key}.jpg") if img else None
        save_specs(key, model, device, specs, hosted or img, score, "ok")
        matched += 1
        print(f"  ok({score})  {model:30} -> {device['full']:32} "
              f"({len(specs or {})} specs) [{key}]")
        time.sleep(DELAY)

    print(f"\nDone. matched={matched} not_found={notfound}")


def dry_run(limit=None):
    print("Loading GSMArena device DB...")
    devices = load_devices()
    print(f"  {len(devices)} devices loaded.")
    todo = _missing_keys()
    keys = list(todo.items())
    if limit:
        keys = keys[:limit]
    print(f"{len(keys)} variant_keys missing specs.\n")
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
            specs, img, url = fetch_device(device)
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
