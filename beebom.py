"""
Beebom image enrichment (gadgets.beebom.com).

Primary card image (specs.image_url): a clean front-back render per phone at
~640-1000px, far larger than GSMArena's 160px. Specs stay on GSMArena; this only
writes image_url + image_source='beebom'.

Matching uses Beebom's OWN full catalog sitemap (all_mobiles.xml, ~2300 slugs),
fetched once and cached. Each of our models is matched LOCALLY to a Beebom slug via
a separator/5G-insensitive compact key (so 'OPPO Reno 11' -> 'oppo-reno11-5g',
'OnePlus Nord CE 4' -> 'oneplus-nord-ce4-5g' all resolve); a few brand-token
variants (apple/iphone, xiaomi/redmi, nothing/cmf, motorola/moto) cover Beebom's
brand-prefix differences. Only the matched product page is fetched, for its
og:image; a URL-pasted-as-filename / geni.us og:image is rejected (Beebom CMS junk).
Unmatched models surface in missing_images for admin upload.

Per MODEL, incremental, dedup. Run:
  python3 beebom.py            # fetch images for models missing one
  python3 beebom.py --dry       # model -> matched slug (or closest), NO writes
  python3 beebom.py --limit N
"""
import os
import re
import sys
import time
import random
import tempfile

from normalize import make_variant_key
from gsmarena import _get, _fetch_all, upsert_specs

SITEMAP = "https://gadgets.beebom.com/all_mobiles.xml"
PROD = "https://gadgets.beebom.com/mobile/"
DELAY = float(os.environ.get("BEEBOM_DELAY") or 1.0)
_CACHE = os.environ.get("BEEBOM_SLUGS_FILE") or os.path.join(
    tempfile.gettempdir(), "beebom_slugs.txt")
_TTL = 7 * 86400


def compact(s):
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return re.sub(r"(5g|4g|3g)", "", s)


def _index_keys(slug):
    full = compact(slug)
    noyear = re.sub(r"20[12][0-9]", "", full)
    return [full, noyear]


def slug_variants(model):
    base = make_variant_key(model, None)
    out = [base]
    def add(s):
        if s and s not in out:
            out.append(s)
    if base.startswith("apple-"):
        add(base[len("apple-"):])
    if re.match(r"^xiaomi-(redmi|poco|mi-)", base):
        add(base[len("xiaomi-"):])
    if base.startswith("nothing-cmf"):
        add(base[len("nothing-"):])
    if base.startswith("motorola-moto-"):
        rest = base[len("motorola-moto-"):]
        add("motorola-" + rest)
        add("moto-" + rest)
    elif base.startswith("motorola-"):
        add("moto-" + base[len("motorola-"):])
    return out


def load_index():
    """compact-key -> beebom slug, from the catalog sitemap (cached a week)."""
    slugs = None
    if os.path.exists(_CACHE) and time.time() - os.path.getmtime(_CACHE) < _TTL:
        slugs = open(_CACHE).read().split()
    if not slugs:
        r = _get(SITEMAP)
        slugs = re.findall(r"/mobile/([a-z0-9\-]+)", r.text) if r and r.status_code == 200 else []
        if slugs:
            try:
                open(_CACHE, "w").write("\n".join(slugs))
            except Exception:
                pass
    idx = {}
    for s in slugs:
        for k in _index_keys(s):
            if k and (k not in idx or len(s) < len(idx[k])):
                idx[k] = s
    return idx, slugs


def match_slug(model, idx):
    for v in slug_variants(model):
        s = idx.get(compact(v))
        if s:
            return s
    return None


def closest(model, slugs, n=3):
    toks = set(make_variant_key(model, None).split("-"))
    scored = []
    for s in slugs:
        sc = len(toks & set(s.split("-")))
        if sc:
            scored.append((sc, s))
    scored.sort(reverse=True)
    return [s for _, s in scored[:n]]


def fetch_image(slug):
    r = _get(PROD + slug)
    if not r or r.status_code != 200:
        return None
    m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)', r.text)
    if not m or "beebom.com" not in m.group(1):
        return None
    img = m.group(1)
    tail = img.split("beebom.com/", 1)[-1].lower()
    if "http" in tail or "geni.us" in tail:        # CMS pasted a URL as the filename
        return None
    return img


def _targets():
    phones = _fetch_all("phones", "model")
    specs = _fetch_all("specs", "model,image_url,image_source")
    have = {(r.get("model") or "").lower() for r in specs
            if r.get("image_source") == "beebom" and r.get("image_url")}
    todo, seen = [], set()
    for p in phones:
        m = p.get("model") or ""
        if not m or m.lower() in seen or m.lower() in have:
            continue
        seen.add(m.lower())
        todo.append(m)
    return todo


def enrich(limit=None):
    from db import host_image
    idx, _ = load_index()
    print(f"  {len(idx)} Beebom slugs indexed.")
    models = _targets()
    models = models[:limit] if limit else models
    print(f"{len(models)} models missing a Beebom image.\n")
    got = miss = 0
    for model in models:
        slug = match_slug(model, idx)
        img = fetch_image(slug) if slug else None
        if not img:
            miss += 1
            print(f"  MISS   {model}")
            continue
        hosted = host_image(img, f"img/{make_variant_key(model, None)}.jpg") or img
        upsert_specs(model, {"image_url": hosted, "image_source": "beebom"})
        got += 1
        print(f"  ok     {model:32} -> {slug}")
        time.sleep(DELAY + random.uniform(0, DELAY))
    print(f"\nDone. images={got} misses={miss}")


def dry_run(limit=None):
    idx, slugs = load_index()
    print(f"  {len(idx)} Beebom slugs indexed.")
    models = _targets()
    models = models[:limit] if limit else models
    print(f"{len(models)} models missing a Beebom image.\n")
    got = miss = 0
    for model in sorted(models):
        slug = match_slug(model, idx)
        if slug:
            got += 1
            print(f"  ok    {model:32} -> {slug}")
        else:
            miss += 1
            print(f"  MISS  {model:32} -> closest: {closest(model, slugs)}")
        time.sleep(0.05)
    total = got + miss
    print(f"\nWOULD images={got} misses={miss}" +
          (f" (match={got/total*100:.0f}%)" if total else ""))


if __name__ == "__main__":
    lim = None
    if "--limit" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--dry" in sys.argv:
        dry_run(lim)
    else:
        from obs import init_sentry, log_error
        init_sentry("beebom")
        try:
            enrich(lim)
        except Exception as e:
            log_error(e, site="beebom", phase="enrich")
            raise
