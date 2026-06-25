#!/usr/bin/env python3
"""Self-healing triage: surface phones GSMArena couldn't match and in-stock
models with no image, each with diagnosis, as a GitHub-issue body (markdown to
stdout). The triage.yml workflow runs this daily and upserts one issue; a
Claude Code session (triggered on the issue) then investigates + proposes fixes.

No writes. DB-only signals (missing images) always work; the GSMArena match
re-check is skipped gracefully if GSMArena blocks the CI IP."""
import statistics
import sys

from db import supabase, _exec
from gsmarena import (_fetch_all, load_devices, load_aliases,
                      match_with_aliases, closest_devices)


def all_models():
    """Every distinct phone model + a set of those currently in stock. We check
    ALL phones (not just in-stock) because a bad/leaked name produces a stray
    variant that's usually OUT OF STOCK — exactly the not_founds we want to catch
    and clean. Returns (sorted list of all models, set of in-stock models)."""
    rows = _fetch_all("phones", "model,in_stock")
    allm, instock = set(), set()
    for r in rows:
        m = (r.get("model") or "").strip()
        if not m:
            continue
        allm.add(m)
        if r.get("in_stock"):
            instock.add(m)
    return sorted(allm), instock


def not_found_set():
    rows = _fetch_all("specs", "model,status")
    return {(r.get("model") or "").lower() for r in rows if r.get("status") == "not_found"}


def missing_images():
    try:
        return _exec(lambda: supabase.table("missing_images").select("*").execute()).data or []
    except Exception:
        return []


def _norm_ram(ram):
    """Match the web's RAM normalisation (lib/queries.js): lowercase, drop spaces
    + the word 'ram', so '12GB'/'12GB RAM'/'12gbram' unify. Returns None if empty."""
    if not ram:
        return None
    r = (ram or "").lower().replace(" ", "").replace("ram", "")
    return r or None


def _is_apple(model):
    m = (model or "").lower()
    return m.startswith("apple") or "iphone" in m or "ipad" in m


def ram_gaps():
    """Android storage variants (variant_key) that have BOTH a null-RAM listing
    AND >=1 real-RAM listing. A store that lists a phone WITHOUT its RAM, on a
    storage that DOES come in multiple RAM configs (e.g. S23 Ultra 256GB = 8/12GB),
    can't be placed under the right per-RAM card — the web folds it into every RAM
    card as a fallback, but the clean fix is the SCRAPER capturing RAM for that
    listing. Apple is excluded (iPhones don't vary RAM within a model). Returns a
    list of dicts sorted by null-RAM in-stock listings desc, [] if none."""
    rows = _fetch_all("phones", "variant_key,model,ram,in_stock")
    by = {}
    for r in rows:
        vk = r.get("variant_key")
        if not vk or _is_apple(r.get("model")):
            continue
        d = by.setdefault(vk, {"model": r.get("model") or "", "rams": set(),
                               "null_total": 0, "null_instock": 0})
        nr = _norm_ram(r.get("ram"))
        if nr:
            d["rams"].add(nr)
        else:
            d["null_total"] += 1
            if r.get("in_stock"):
                d["null_instock"] += 1
    out = [{"variant_key": vk, "model": d["model"], "ram_configs": len(d["rams"]),
            "null_total": d["null_total"], "null_instock": d["null_instock"]}
           for vk, d in by.items() if len(d["rams"]) >= 2 and d["null_total"] > 0]
    out.sort(key=lambda x: (-x["null_instock"], -x["ram_configs"], x["model"].lower()))
    return out


def scraper_health():
    """Per-site yield anomalies from scrape_runs (silent breakage). Returns a list
    of (site, reason), [] if all healthy, or None if the table isn't populated."""
    try:
        rows = _exec(lambda: supabase.table("scrape_runs")
                     .select("site,seen_count,run_complete,run_at")
                     .order("run_at", desc=True).limit(3000).execute()).data
    except Exception:
        return None
    if not rows:
        return None
    by = {}
    for r in rows:
        by.setdefault(r["site"], []).append(r)
    flags = []
    for site, rs in by.items():
        latest = rs[0]                       # query is newest-first
        seen = latest.get("seen_count") or 0
        prior = [x["seen_count"] for x in rs[1:13] if x.get("seen_count") is not None]
        med = statistics.median(prior) if prior else None
        if seen == 0:
            flags.append((site, "0 phones seen this run — parser likely broken (site HTML changed?)"))
        elif latest.get("run_complete") is False:
            flags.append((site, f"run reported incomplete ({seen} seen) — partial/blocked"))
        elif med and seen < 0.5 * med:
            flags.append((site, f"yield dropped to {seen} (recent median {int(med)})"))
    flags.sort()
    return flags


def main():
    nf = not_found_set()
    models, instock = all_models()

    # Re-verify EVERY not_found (in-stock + OOS) against the live device DB. A
    # stale not_found that WOULD match now is dropped (self-heals next enrich);
    # the rest are real misses — usually a name leak that left a stray OOS
    # variant, which is exactly what we want to surface + fix in clean_model.
    # In-stock ones are listed first (higher priority).
    unmatched, gsm_note = [], None
    try:
        devices = load_devices()
        aliases = load_aliases()
        for m in models:
            if m.lower() not in nf:
                continue
            device, _ = match_with_aliases(m, devices, aliases)
            if device:
                continue
            unmatched.append((m, m in instock, closest_devices(m, devices)))
        # in-stock first, then alphabetical
        unmatched.sort(key=lambda u: (not u[1], u[0].lower()))
    except Exception as e:
        gsm_note = f"GSMArena device DB unreachable from CI ({e}); run `python3 gsmarena.py --audit` locally to diagnose matches."

    imgs = missing_images()

    out = []
    out.append("## 🔧 Self-healing triage\n")
    out.append(f"_Auto-generated daily. {len(unmatched)} unmatched spec(s), "
               f"{len(imgs)} model(s) with no image._\n")

    out.append("### Phones GSMArena couldn't match (all stock states)\n")
    if gsm_note:
        out.append(f"> {gsm_note}\n")
    elif unmatched:
        n_instock = sum(1 for u in unmatched if u[1])
        out.append(f"{n_instock} in stock, {len(unmatched) - n_instock} out of stock. "
                   "Likely a name-normalization leak (fix `clean_model()`/`COLORS` or add "
                   "a `model_aliases` row), a non-phone (add to `NON_PHONE_KEYWORDS`), or "
                   "genuinely absent on GSMArena (admin image upload). OOS ones are usually "
                   "stray leaked-name variants — fixing clean_model merges them away.\n")
        out.append("| Our model | Stock | Closest GSMArena names |")
        out.append("|---|---|---|")
        for m, in_stock, cand in unmatched:
            out.append(f"| {m} | {'in stock' if in_stock else 'OOS'} | {', '.join(cand) or '—'} |")
    else:
        out.append("None — every phone is matched. 🎉")

    out.append("\n### In-stock models with no image\n")
    if imgs:
        out.append("These need a canonical image (scraper match or admin upload).\n")
        out.append("| Model | Sample name | Offers |")
        out.append("|---|---|---|")
        for r in imgs[:100]:
            out.append(f"| {r.get('model','')} | {r.get('sample_name','')} | {r.get('offer_count','')} |")
        if len(imgs) > 100:
            out.append(f"\n_…and {len(imgs) - 100} more._")
    else:
        out.append("None. 🎉")

    out.append("\n### Android RAM gaps (null RAM on a multi-RAM storage)\n")
    gaps = ram_gaps()
    if gaps:
        out.append("These Android storage variants come in >1 RAM config but ALSO "
                   "have listing(s) with NO RAM captured, so the no-RAM offer can't "
                   "be placed under the right per-RAM card. Fix = capture RAM in the "
                   "store's scraper for these (or add the RAM to the listing's name). "
                   "Apple is excluded (iPhones don't vary RAM within a model).\n")
        out.append("| Variant key | Model | RAM configs | No-RAM (in stock) |")
        out.append("|---|---|---|---|")
        for g in gaps[:60]:
            out.append(f"| `{g['variant_key']}` | {g['model']} | {g['ram_configs']} | "
                       f"{g['null_total']} ({g['null_instock']}) |")
        if len(gaps) > 60:
            out.append(f"\n_…and {len(gaps) - 60} more._")
    else:
        out.append("None. 🎉")

    out.append("\n### Scraper health\n")
    health = scraper_health()
    if health is None:
        out.append("_No run data yet (the `scrape_runs` table fills on the next scrape)._")
    elif health:
        out.append("Scrapers whose latest run looks broken — usually a site HTML "
                   "change; fix the matching scraper file.\n")
        out.append("| Scraper | Signal |")
        out.append("|---|---|")
        for site, reason in health:
            out.append(f"| `{site}.py` | {reason} |")
    else:
        out.append("All scrapers yielding normally. 🎉")

    out.append("\n---")
    out.append("_Reply on this issue to direct a fix — e.g. \"add alias X\", "
               "\"non-phone, filter it\", \"upload image for Y\", \"scraper Z broke\" — "
               "and I'll open a PR._")
    print("\n".join(out))


if __name__ == "__main__":
    main()
