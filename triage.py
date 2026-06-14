#!/usr/bin/env python3
"""Self-healing triage: surface phones GSMArena couldn't match and in-stock
models with no image, each with diagnosis, as a GitHub-issue body (markdown to
stdout). The triage.yml workflow runs this weekly and upserts one issue; a
Claude Code session (triggered on the issue) then investigates + proposes fixes.

No writes. DB-only signals (missing images) always work; the GSMArena match
re-check is skipped gracefully if GSMArena blocks the CI IP."""
import sys

from db import supabase, _exec
from gsmarena import (_fetch_all, load_devices, load_aliases,
                      match_with_aliases, closest_devices)


def in_stock_models():
    rows = _fetch_all("phones", "model,in_stock")
    return sorted({(r.get("model") or "") for r in rows if r.get("in_stock")} - {""})


def not_found_set():
    rows = _fetch_all("specs", "model,status")
    return {(r.get("model") or "").lower() for r in rows if r.get("status") == "not_found"}


def missing_images():
    try:
        return _exec(lambda: supabase.table("missing_images").select("*").execute()).data or []
    except Exception:
        return []


def main():
    nf = not_found_set()
    stock = in_stock_models()

    # Re-verify each in-stock not_found against the live device DB (a stale
    # not_found that WOULD match now is dropped — it self-heals next enrich).
    unmatched, gsm_note = [], None
    try:
        devices = load_devices()
        aliases = load_aliases()
        for m in stock:
            if m.lower() not in nf:
                continue
            device, _ = match_with_aliases(m, devices, aliases)
            if device:
                continue
            unmatched.append((m, closest_devices(m, devices)))
    except Exception as e:
        gsm_note = f"GSMArena device DB unreachable from CI ({e}); run `python3 gsmarena.py --audit` locally to diagnose matches."

    imgs = missing_images()

    out = []
    out.append("## 🔧 Self-healing triage\n")
    out.append(f"_Auto-generated weekly. {len(unmatched)} unmatched spec(s), "
               f"{len(imgs)} model(s) with no image._\n")

    out.append("### Phones GSMArena couldn't match (in stock)\n")
    if gsm_note:
        out.append(f"> {gsm_note}\n")
    elif unmatched:
        out.append("Likely a name-normalization gap (fix `clean_model()` or add a "
                   "`model_aliases` row), a non-phone (add to `NON_PHONE_KEYWORDS`), "
                   "or genuinely absent on GSMArena (admin image upload).\n")
        out.append("| Our model | Closest GSMArena names |")
        out.append("|---|---|")
        for m, cand in unmatched:
            out.append(f"| {m} | {', '.join(cand) or '—'} |")
    else:
        out.append("None — every in-stock phone is matched. 🎉")

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

    out.append("\n---")
    out.append("_Reply on this issue to direct a fix — e.g. \"add alias X\", "
               "\"non-phone, filter it\", \"upload image for Y\" — and I'll open a PR._")
    print("\n".join(out))


if __name__ == "__main__":
    main()
