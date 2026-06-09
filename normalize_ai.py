"""
AI-assisted name normalizer (Claude, propose-only).

Why this exists
---------------
The deterministic pipeline (clean_model + normalize_db.py Passes 0/1/2) plus the
GSMArena/Beebom matchers already handle the vast majority of names. What they
CAN'T reliably judge is the genuinely-ambiguous residue:
  - "Vivo V60e Noble"     -> is "Noble" a colour leak or part of the name?
  - "Motorola Edge 60 Stylus" -> a real distinct variant, or "Edge 60" + junk?
  - "iPhone SE 2020" vs "iPhone SE 2" -> the same physical phone under two names?
Store-count consensus gets the Stylus case WRONG (a real phone sold by 1 store
looks like noise). That judgment — "is this a real phone name?" — is what Claude
is good at, IF we stop it from inventing phones.

Balanced design (avoids both old failure modes)
-----------------------------------------------
v1 was "too wild" because it emitted free-text rewrites (it could name a phone
that doesn't exist). v2 "did nothing" because the guardrails were a hard
allow-list (zero recall). The fix is to change WHAT the model may emit:

  1. GROUNDED OUTPUT. For every candidate the model returns exactly one of
     three bounded actions — strip / alias / keep — and any target it names for
     strip/alias MUST already exist in our DB or in the GSMArena device list.
     We validate that here and DROP any suggestion that doesn't resolve. The
     model physically cannot introduce a phone that isn't real.
  2. SMALL CANDIDATE SET. We don't ask about the whole DB — only the residue the
     deterministic + GSMArena passes couldn't settle (~tens of names/run).
  3. GSMArena AS GROUND TRUTH IN THE PROMPT. Each candidate ships with its top
     GSMArena fuzzy matches, so the model picks among REAL devices we show it
     rather than recalling (and confusing) variants from training.

PROPOSE-ONLY. This never touches phones.model / variant_key. Every suggestion is
written to the `normalize_review` table for manual promotion (see
normalize_review_schema.sql). Run AFTER normalize_db.py.

Usage:
  python3 normalize_ai.py            # propose into normalize_review
  python3 normalize_ai.py --dry      # print proposals, write nothing
  python3 normalize_ai.py --limit N  # cap candidates (testing)
"""
import os
import re
import sys
import json
from dotenv import load_dotenv
from supabase import create_client

from normalize import clean_model
from obs import init_sentry, log_error

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

MODEL = os.environ.get("NORMALIZE_AI_MODEL", "claude-sonnet-4-6")
BATCH = int(os.environ.get("NORMALIZE_AI_BATCH", "40"))

# Variant-line words: if a model's trailing token is one of these it's almost
# certainly a real different phone, not a colour leak — never even a candidate.
_PROTECT = set((
    "pro max ultra plus lite neo turbo note fe mini fold flip edge ce air se prime "
    "power ace active master racing explorer champion speed gt zoom fusion stylus "
    "play nord narzo 5g 4g"
).split())


def fetch_phones():
    rows, start = [], 0
    while True:
        chunk = (sb.table("phones")
                 .select("model, site")
                 .order("id").range(start, start + 999).execute().data)
        rows += chunk
        if len(chunk) < 1000:
            break
        start += 1000
    return rows


def build_candidates(phones, devices, limit=None):
    """The ambiguous residue worth a model call. A model is a candidate if either:
      (a) GSMArena does NOT recognize it AND its last token is a plain alpha word
          (a likely colour/junk leak — but could be a real variant), or
      (b) it's a longer form of another model we also carry (possible alias/leak).
    Everything GSMArena already confirms is skipped — no spend on settled names."""
    from gsmarena import best_match, closest_devices

    model_sites = {}
    for p in phones:
        m = (p.get("model") or "").strip()
        if m:
            model_sites.setdefault(m, set()).add(p.get("site"))
    models = set(model_sites)

    cands = []
    for m in sorted(models):
        toks = m.split()
        if len(toks) < 2:
            continue
        last = toks[-1].lower()
        recognized = best_match(m, devices)[0] is not None

        suspicious_tail = (re.fullmatch(r"[a-z]+", last) and last not in _PROTECT)
        base = " ".join(toks[:-1])
        longer_form = base in models

        if recognized and not longer_form:
            continue                       # settled by GSMArena; skip
        if not (suspicious_tail or longer_form):
            continue

        cands.append({
            "name": m,
            "sites": sorted(model_sites[m]),
            "n_sites": len(model_sites[m]),
            "base_in_db": base if longer_form else None,
            "base_n_sites": len(model_sites.get(base, ())) if longer_form else 0,
            "gsm_recognized": recognized,
            "gsm_candidates": closest_devices(m, devices, n=4),
        })
        if limit and len(cands) >= limit:
            break
    return cands, models


SYSTEM = """You normalize refurbished-phone model names for a price-comparison site.
Each store names the SAME physical phone slightly differently; we group offers by a
storage-only key derived from the model name, so the name must be the phone's real
commercial name with NO store-specific noise (colour words, condition grades, SIM/
network tags, marketing fluff) and NO invented variants.

For each candidate choose exactly ONE action:
  - "strip": the name has a trailing store-specific token (a COLOUR like Noble/
    Glacier/Aqua/Nebula, or junk like "Sim Slot"/"Dual"/"Fair"). Return the
    correct shorter name in `target`. Only if the shorter name is a real phone.
  - "alias": the name is the SAME physical phone as a DIFFERENTLY-NAMED real model
    (e.g. "iPhone SE 2020" == "iPhone SE 2"). Put the canonical name in `target`.
  - "keep": the name is already a correct, real, distinct phone — INCLUDING genuine
    variants sold by only one store (e.g. "Motorola Edge 60 Stylus" is a real phone,
    NOT "Edge 60" plus junk). When unsure, choose "keep".

Rules:
- `target` for strip/alias MUST be a real phone. Prefer a name shown in the
  candidate's GSMArena matches or its store-base. NEVER invent a model.
- Variant words (Pro, Max, Ultra, Plus, Lite, Neo, FE, CE, Edge, Fold, Flip,
  Stylus, Note, Air, SE, GT, Nord, Narzo) are REAL — never strip them.
- A lone trailing word being uncommon is not proof it's noise. If it could be a
  real model line, keep.
- Output STRICT JSON only: {"results":[{"name","action","target","confidence","reason"}]}
  confidence 0..1; target null for keep; reason one short clause."""


def ask_claude(client, batch, devices):
    from gsmarena import best_match

    lines = []
    for c in batch:
        ctx = "; ".join(c["gsm_candidates"]) or "(none)"
        base = (f' | also-in-DB shorter form "{c["base_in_db"]}" '
                f'({c["base_n_sites"]} stores)' if c["base_in_db"] else "")
        lines.append(
            f'- "{c["name"]}" | {c["n_sites"]} store(s)'
            f'{base} | GSMArena: {"recognized" if c["gsm_recognized"] else "NOT recognized"}'
            f' | closest real devices: {ctx}')
    user = ("Candidates:\n" + "\n".join(lines) +
            "\n\nReturn JSON for every candidate by exact `name`.")

    resp = client.messages.create(
        model=MODEL, max_tokens=4000, system=SYSTEM,
        messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model reply: {text[:200]}")
    data = json.loads(m.group(0))

    # Grounded validation: drop any strip/alias whose target isn't a real phone.
    valid_names = {c["name"] for c in batch}
    out = []
    for r in data.get("results", []):
        name = r.get("name")
        action = r.get("action")
        target = (r.get("target") or "").strip() or None
        if name not in valid_names or action not in ("strip", "alias", "keep"):
            continue
        if action in ("strip", "alias"):
            if not target:
                continue
            target = clean_model(target)
            grounded = (target in ALL_MODELS) or (best_match(target, devices)[0] is not None)
            if not grounded:
                r["_dropped"] = "target not a known/real phone"
                out.append((r, False))
                continue
            r["target"] = target
        out.append((r, True))
    return out


ALL_MODELS = set()


def run(dry=False, limit=None):
    global ALL_MODELS
    print("Fetching phones...")
    phones = fetch_phones()
    from gsmarena import load_devices
    devices = load_devices()
    print(f"  {len(phones)} rows, {len(devices)} GSMArena devices")

    cands, ALL_MODELS = build_candidates(phones, devices, limit=limit)
    print(f"  {len(cands)} ambiguous candidates for the model")
    if not cands:
        print("Nothing to review.")
        return

    from anthropic import Anthropic
    client = Anthropic()   # ANTHROPIC_API_KEY from env

    proposed = 0
    for i in range(0, len(cands), BATCH):
        batch = cands[i:i + BATCH]
        print(f"\n── batch {i // BATCH + 1} ({len(batch)} names) ──")
        try:
            results = ask_claude(client, batch, devices)
        except Exception as e:
            log_error(e, component="normalize_ai", phase="ask")
            print(f"  batch error: {e}")
            continue
        by_name = {c["name"]: c for c in batch}
        for r, ok in results:
            tag = "" if ok else f"  [DROPPED: {r.get('_dropped')}]"
            print(f"  {r['name']!r} -> {r['action']}"
                  f"{(' ' + repr(r.get('target'))) if r.get('target') else ''}"
                  f" ({r.get('confidence')}) — {r.get('reason')}{tag}")
            if not ok or r["action"] == "keep" or dry:
                continue
            c = by_name[r["name"]]
            row = {
                "name": r["name"], "proposed_action": r["action"],
                "target": r.get("target"),
                "confidence": r.get("confidence"),
                "reason": (r.get("reason") or "")[:500],
                "sample_sites": ", ".join(c["sites"]),
                "gsm_context": "; ".join(c["gsm_candidates"]),
                "status": "pending",
            }
            try:
                sb.table("normalize_review").upsert(
                    row, on_conflict="name,proposed_action,target").execute()
                proposed += 1
            except Exception as e:
                print(f"    write error: {e}")
    print(f"\n✓ {proposed} suggestions queued to normalize_review"
          + (" (dry run: nothing written)" if dry else ""))


if __name__ == "__main__":
    init_sentry("normalize_ai")
    dry = "--dry" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    try:
        run(dry=dry, limit=limit)
    except Exception as e:
        log_error(e, component="normalize_ai", phase="run")
        raise
