# Handoff — diagnose broken scrapers (new session, network allowlist added)

## Why this session exists
Triage issue **aneemtree/phone-scraper#7** flags scrapers returning **0 phones**.
The previous session's sandbox couldn't reach store sites (egress blocked), so it
could only classify them by knowledge, not by fetching. This session should have
the store hosts **allowlisted** (Custom network access), so you can fetch live
payloads and classify each break: **parser change** (site HTML/API changed → fix
code) vs **403/WAF block** (datacenter IP → needs SCRAPER_PROXY, not a code fix).

## First: confirm network works
Run a quick reachability check before anything else:
```bash
for h in gadgetrebirth.com api.gadgetrebirth.com gudfast.com cellbuddy.in itradeit.in \
         gadgets.beebom.com www.gsmarena.com zublxmgjnsqjoztcojst.supabase.co; do
  echo -n "$h -> "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 15 "https://$h/" || echo FAIL
done
```
If these 000/FAIL, the allowlist didn't apply — stop and tell the user.

## Repo state / branch (IMPORTANT)
- Work branch: **`claude/nice-tesla-qftYE`** (both repos). Check it out first:
  `git fetch origin claude/nice-tesla-qftYE && git checkout claude/nice-tesla-qftYE`
- This branch has UNMERGED scraper work (do NOT lose it): colour-leak COLORS
  additions, the **auto-growing Beebom colour vocab** (normalize_db.build_color_vocab
  + normalize.set_dynamic_colors), and a **laptop NON_PHONE_KEYWORDS** filter
  (dell/latitude/thinkpad/…/inch) + "supernova". All validated, none merged to main.
- NEVER merge to `main` without the user's explicit "merge with main". Develop +
  commit + push on the branch. Commit footer must include:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and
  `Claude-Session: <this session's url>`.

## The broken scrapers (from triage #7, "Scraper health")
1. **gadgetrebirth.py** — 0 phones. NEW break (was working). Own JSON API at
   `https://api.gadgetrebirth.com/api/products?limit=100&skip=<n>`, category=="phones".
2. **gudfast.py** — 0 phones. NEW break. WooCommerce Store API
   `https://gudfast.com/wp-json/wc/store/v1/products?category=123` + is_phone().
3. **cellbuddy.py** — 0 phones. KNOWN Cloudflare-WAF 403 to datacenter IPs; needs
   the `SCRAPER_PROXY` secret (residential). Confirm it's still 403 (not a new parser break).
4. **itradeit.py** — 0 phones. KNOWN datacenter-IP 403 (residential works); confirm still 403.

## How to diagnose (no DB writes needed)
Most scrapers have a `--dry` mode that fetches + parses + prints offers with NO DB:
```bash
python3 gadgetrebirth.py --dry        # expect offers printed; if 0, inspect why
python3 gudfast.py --dry --oos        # db imported lazily; --oos includes sold-out
```
cellbuddy/itradeit have no --dry; fetch their listing endpoint directly with curl to
see the HTTP status + first bytes, e.g.:
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://gudfast.com/wp-json/wc/store/v1/products?category=123&per_page=1"
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://api.gadgetrebirth.com/api/products?limit=1&skip=0"
```
Classify each:
- **200 + payload but scraper prints 0** → parser/shape change. Diff the live JSON
  against what the scraper expects (category key, pagination, field names) and fix
  the parse. Validate with `--dry` before pushing.
- **403 / challenge HTML** → WAF/IP block. NOT a code bug. Note it needs
  SCRAPER_PROXY (HTTPS_PROXY on that scraper's CI steps). Don't "fix" the parser.
- **Endpoint 404 / moved** → the store changed URL/structure; find the new endpoint.

## Env the scrapers may need
- `--dry` paths generally need NO secrets. A full run needs `SUPABASE_URL`,
  `SUPABASE_SERVICE_KEY` (+ R2_* for images) in a `.env`. Ask the user for a `.env`
  if you need end-to-end; for diagnosis, `--dry` + curl is enough.
- To cross-check the catalog/specs you can use the Supabase MCP (project
  `zublxmgjnsqjoztcojst`) — that path doesn't depend on the sandbox network.

## Also pending on the branch (context, not this session's job unless asked)
- Colour + laptop + RAM normalization fixes are branch-only. Once the user says
  "merge with main", merge both repos' `claude/nice-tesla-qftYE`, then trigger the
  `normalize.yml` workflow so existing rows self-heal (Pass 1 + Pass 2), and update
  each repo's CLAUDE.md in the same commit (that's the repo convention).
- Non-network triage items still open: Moto-series gsmarena matcher gap (needs live
  GSMArena, now reachable — `python3 gsmarena.py --dry --limit N`), iPhone SE 2016
  needs a `model_aliases` row, long-tail old phones genuinely absent.

## Deliverable
For each of the 4 scrapers: a one-line verdict (parser-fix vs proxy-needed vs
endpoint-moved) with the evidence (HTTP code / payload snippet), and for the
parser-fix ones, the fix committed to `claude/nice-tesla-qftYE` and validated with
`--dry`. Report verdicts back; don't merge to main.
