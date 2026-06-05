"""
Beebom image enrichment (gadgets.beebom.com).

Source of the PRIMARY card image (specs.image_url). Beebom hosts a clean
front-back product render per phone at ~640-1000px (vs GSMArena's 160px), reachable
by a predictable slug: gadgets.beebom.com/mobile/<slug>, og:image -> the render on
cdn.beebom.com. Open access (no bot block), so requests-only by URL construction;
no full-catalog crawl needed.

Per MODEL (not variant_key), incremental: only models without a Beebom image are
processed, each fetched once and shared across storage variants via the offers
view's model join. Specs stay on GSMArena; this only writes image_url +
image_source='beebom'. Misses (slug not found) keep no image and surface in
missing_images for admin upload (gsmarena.set_image).

Run:  python3 beebom.py            # fetch images for models missing one
      python3 beebom.py --dry       # print matches/coverage, NO writes
      python3 beebom.py --limit N
"""
import re
import sys
import time
import random

from normalize import make_variant_key
from gsmarena import _get, _fetch_all, upsert_specs, _toks

BASE = "https://gadgets.beebom.com/mobile/"
DELAY = float(__import__("os").environ.get("BEEBOM_DELAY") or 1.0)


def slug_candidates(model):
    base = make_variant_key(model, None)
    cands = [base]
    def add(s):
        if s and s not in cands:
            cands.append(s)
    if base.startswith("apple-"):
        add(base[len("apple-"):])
    if re.match(r"^xiaomi-(redmi|poco|mi-)", base):
        add(re.sub(r"^xiaomi-", "", base))
    if base.startswith("nothing-cmf"):
        add(base[len("nothing-"):])
    if "-ce-" in base:
        add(re.sub(r"-ce-(\d)", r"-ce\1", base))
    if re.search(r"-(fold|flip)-\d", base):
        add(re.sub(r"-(fold|flip)-(\d)", r"-\1\2", base))
    if base.startswith("motorola-moto-"):
        rest = base[len("motorola-moto-"):]
        add("motorola-" + rest)
        add("moto-" + rest)
    elif base.startswith("motorola-"):
        add("moto-" + base[len("motorola-"):])
    for c in list(cands):                     # also try the 5G-suffixed form of each
        add(c + "-5g")
    return cands


def _page_is_model(model, html):
    """Guard against slug collisions: the page title must contain the model's
    non-brand tokens (so e.g. an 'oppo-reno-10' slug landing on a Honor page is
    rejected). Does NOT catch a correct page with a wrong CMS image."""
    m = (re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)', html)
         or re.search(r'<title[^>]*>([^<]+)', html))
    if not m:
        return False
    title = set(_toks(m.group(1)))
    ours = _toks(model)
    rest = set(ours[1:]) if len(ours) > 1 else set(ours)
    return rest <= title


def fetch_image(model):
    """Return (image_url, page_url) from Beebom, or (None, None)."""
    for slug in slug_candidates(model):
        url = BASE + slug
        r = _get(url)
        if not r or r.status_code != 200 or not _page_is_model(model, r.text):
            continue
        m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)', r.text)
        if m and "beebom.com" in m.group(1):
            return m.group(1), url
    return None, None


def _targets():
    """Distinct phone MODELS with no Beebom image yet."""
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
    models = _targets()
    models = models[:limit] if limit else models
    print(f"{len(models)} models missing a Beebom image.\n")
    got = miss = 0
    for model in models:
        img, page = fetch_image(model)
        if not img:
            miss += 1
            print(f"  MISS   {model}")
            continue
        hosted = host_image(img, f"img/{make_variant_key(model, None)}.jpg") or img
        upsert_specs(model, {"image_url": hosted, "image_source": "beebom"})
        got += 1
        print(f"  ok     {model:34} {hosted}")
        time.sleep(DELAY + random.uniform(0, DELAY))
    print(f"\nDone. images={got} misses={miss}")


def dry_run(limit=None):
    models = _targets()
    models = models[:limit] if limit else models
    print(f"{len(models)} models missing a Beebom image.\n")
    got = miss = 0
    for model in sorted(models):
        img, page = fetch_image(model)
        if img:
            got += 1
            print(f"  ok    {model:34} {img}")
        else:
            miss += 1
            print(f"  MISS  {model}")
        time.sleep(DELAY)
    total = got + miss
    print(f"\nWOULD images={got} misses={miss}" +
          (f" (coverage={got/total*100:.0f}%)" if total else ""))


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
