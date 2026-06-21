"""
Beebom enrichment (gadgets.beebom.com) — PRIMARY image + specs.

Per matched MODEL (one GET) we write BOTH:
  - image_url (image_source='beebom'): a clean front-back render at ~640-1000px.
  - specs (JSONB): Beebom's full, India-specific spec sheet in its NATIVE grouped
    form {"_source":"beebom","_groups":[{title,rows:[[label,value]]}], "net5g"?}.
    The web renders the groups directly; net5g (Network->Technology "5G,…") is
    lifted to the top level so the 5G filter works.
Beebom is the PRIMARY spec source (no throttling, India catalog/names). GSMArena
(gsmarena.py) now only BACKFILLS: its _targets skips any model that already has a
`specs` row, so models Beebom matched are left alone and only Beebom-missed (older)
models get GSMArena specs. beebom.py already runs before gsmarena in the workflows.

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
from gsmarena import _get, _fetch_all, upsert_specs, load_aliases

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
    if base.startswith("nothing-cmf"):
        add(base[len("nothing-"):])
    # Xiaomi family: Beebom mixes xiaomi-/mi-/redmi-/poco- prefixes inconsistently.
    if base.startswith("xiaomi-mi-"):
        rest = base[len("xiaomi-mi-"):]
        add("mi-" + rest)                 # mi-11x
        add("xiaomi-" + rest)             # xiaomi-11-lite-ne
    elif base.startswith("xiaomi-redmi-"):
        add(base[len("xiaomi-"):])        # redmi-note-13
    elif base.startswith("xiaomi-poco-"):
        add(base[len("xiaomi-"):])        # poco-f5
    elif base.startswith("xiaomi-"):
        add("xiaomi-mi-" + base[len("xiaomi-"):])   # xiaomi-mi-11-lite
    if base.startswith("poco-"):
        add("xiaomi-" + base)             # xiaomi-poco-f5
    if base.startswith("redmi-"):
        add("xiaomi-" + base)
    # Motorola: Beebom uses both "moto-" and "motorola-moto-".
    if base.startswith("motorola-moto-"):
        rest = base[len("motorola-moto-"):]
        add("motorola-" + rest)
        add("moto-" + rest)
    elif base.startswith("motorola-"):
        rest = base[len("motorola-"):]
        add("moto-" + rest)
        add("motorola-moto-" + rest)
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


def match_slug(model, idx, aliases=None):
    for name in [model] + ((aliases or {}).get(model.lower(), [])):
        for v in slug_variants(name):
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


def fetch_page(slug):
    """One GET per matched product — returns (html, og_image|None) so a single
    fetch yields both the image and the spec sheet."""
    r = _get(PROD + slug)
    if not r or r.status_code != 200:
        return None, None
    html = r.text
    img = None
    m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)', html)
    if m and "beebom.com" in m.group(1):
        tail = m.group(1).split("beebom.com/", 1)[-1].lower()
        if "http" not in tail and "geni.us" not in tail:
            img = m.group(1)
    return html, img


# Beebom renders its full spec sheet as React divs (hashed class names), grouped
# by category (<h3>General/Display/Body/Processor/Main Camera/Operating System/
# Selfie Camera/Battery/Network</h3>) with <li><span>Label</span><span>Value</span>
# rows. We parse by STRUCTURE (h3 split + per-li two-span), not class names. The
# grouped form is stored as-is in specs._groups (the web renders it directly);
# net5g (the 5G-filter signal) is lifted to the top level from Network→Technology.
def _txt(s):
    import html as _h
    return re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_specs(page):
    """Beebom product HTML -> (specs_dict, ok). specs_dict =
    {"_source":"beebom", "_groups":[{title, rows:[[label,value],...]}], "net5g"?}."""
    if not page:
        return None, False
    s = page.find('id="general"')
    e = page.find('id="go-to-store"')
    region = page[s:e] if s >= 0 and e > s else page
    # Split on category headings; parts = [pre, title1, body1, title2, body2, ...]
    parts = re.split(r"<h3[^>]*>(.*?)</h3>", region, flags=re.S)
    groups, net5g = [], None
    for i in range(1, len(parts) - 1, 2):
        title = _txt(parts[i])
        if not title or len(title) > 40:
            continue
        rows = []
        for lab, val in re.findall(
            r"<li[^>]*>\s*<span[^>]*>([^<]+)</span>\s*<span[^>]*>(.*?)</span>\s*</li>",
            parts[i + 1], re.S):
            L, V = _txt(lab), _txt(val)
            if L and V and V != "-":
                rows.append([L, V])
        if rows:
            groups.append({"title": title, "rows": rows})
        if re.search(r"network|connectivity", title, re.I):
            for L, V in rows:
                if re.search(r"technolog|network|speed|band", L, re.I) and "5g" in V.lower():
                    net5g = V
                    break
    if not groups:
        return None, False
    out = {"_source": "beebom", "_groups": groups}
    if net5g:
        out["net5g"] = net5g
    return out, True


def _targets():
    phones = _fetch_all("phones", "model")
    specs = _fetch_all("specs", "model,image_url,image_source")
    # Self-limiting (like gsmarena's not_found): skip models that already have a
    # Beebom image AND those recorded as a miss, so running after every scrape
    # only fetches NEW models — not re-hammering hundreds of unmatched ones.
    have = {(r.get("model") or "").lower() for r in specs
            if r.get("image_source") in ("beebom", "beebom_miss")}
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
    aliases = load_aliases()
    print(f"  {len(idx)} Beebom slugs indexed.")
    models = _targets()
    models = models[:limit] if limit else models
    print(f"{len(models)} models missing a Beebom image.\n")
    got = miss = specs_n = 0
    for model in models:
        slug = match_slug(model, idx, aliases)
        page, img = fetch_page(slug) if slug else (None, None)
        if not page:
            miss += 1
            upsert_specs(model, {"image_source": "beebom_miss"})  # don't retry next run
            print(f"  MISS   {model}")
            continue
        fields = {}
        if img:
            hosted = host_image(img, f"img/{make_variant_key(model, None)}.jpg") or img
            fields["image_url"] = hosted
            fields["image_source"] = "beebom"
        specs, ok = parse_specs(page)
        if ok:
            fields["specs"] = specs   # primary spec source; GSMArena backfills the rest
            specs_n += 1
        if not fields:
            miss += 1
            upsert_specs(model, {"image_source": "beebom_miss"})
            print(f"  MISS   {model} (matched {slug} but no image/specs)")
            continue
        upsert_specs(model, fields)
        got += 1
        print(f"  ok     {model:32} -> {slug}  [{'img' if img else '---'}|{'specs' if ok else '-----'}]")
        time.sleep(DELAY + random.uniform(0, DELAY))
    print(f"\nDone. matched={got} (specs={specs_n}) misses={miss}")


def dry_run(limit=None):
    idx, slugs = load_index()
    aliases = load_aliases()
    print(f"  {len(idx)} Beebom slugs indexed.")
    models = _targets()
    models = models[:limit] if limit else models
    print(f"{len(models)} models missing a Beebom image.\n")
    got = miss = 0
    for model in sorted(models):
        slug = match_slug(model, idx, aliases)
        if slug:
            got += 1
            # When a small --limit is given, fetch + parse to VALIDATE spec
            # extraction (group/row counts + net5g), no DB writes.
            if limit:
                page, img = fetch_page(slug)
                specs, ok = parse_specs(page)
                ng = len(specs["_groups"]) if ok else 0
                nr = sum(len(g["rows"]) for g in specs["_groups"]) if ok else 0
                print(f"  ok    {model:30} -> {slug:34} img={'y' if img else 'n'} "
                      f"groups={ng} rows={nr} 5g={'y' if ok and specs.get('net5g') else 'n'}")
            else:
                print(f"  ok    {model:32} -> {slug}")
        else:
            miss += 1
            print(f"  MISS  {model:32} -> closest: {closest(model, slugs)}")
        time.sleep(0.05)
    total = got + miss
    print(f"\nWOULD match={got} misses={miss}" +
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
