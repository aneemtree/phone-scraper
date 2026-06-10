"""
Auto news blog pipeline: Google Alerts RSS -> clustered stories -> AI-written
original posts with a stock image -> Supabase blog_posts (rendered at /blog on
the website).

Flow per run (news.yml cron):
  1. Load active feeds from the news_feeds table (Google Alerts "deliver to RSS"
     URLs the product owner pastes in).
  2. Fetch + parse each Atom feed; unwrap Google's redirect links to the real
     article URL; drop articles already in news_articles (cross-run URL dedup).
  3. Cluster the remaining new articles by title similarity (same story covered
     by multiple outlets clubs into ONE cluster).
  4. For each cluster, fetch the FULL article text from each source (trafilatura
     extraction — posts are written from the articles themselves, never from the
     alert snippets). Clusters with no fetchable source are skipped WITHOUT
     recording the articles, so they retry on the next run.
  5. Claude (claude-haiku-4-5, structured JSON output) writes an ORIGINAL
     article (title + paragraphs + an image search query). The prompt includes
     the recent posts' titles: if the story was already covered in an earlier
     run (resurfaced via another outlet), the model returns duplicate_of=<slug>
     instead of an article — we then attach the new sources to that existing
     post rather than writing a second one.
  6. A landscape stock photo is fetched from Pexels using the model's image
     query, hosted on R2 at blog/<slug>.jpg (host_image), and credited.
  7. The post is inserted into blog_posts; its articles are recorded in
     news_articles with post_id.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (db.py), ANTHROPIC_API_KEY,
     PEXELS_API_KEY (optional — posts go out imageless without it),
     R2_* (optional — falls back to hotlinking the Pexels CDN URL).

`python3 news.py --dry` runs steps 1-4 and prints the clusters with NO Claude /
Pexels / DB writes (article fetches still happen).
"""
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

from obs import init_sentry, log_error

ATOM = "{http://www.w3.org/2005/Atom}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MODEL = "claude-haiku-4-5"
RECENT_POST_DAYS = 14      # window for "did we already cover this story?"
MAX_SOURCE_CHARS = 4000    # per-article text passed to the model
MAX_TOTAL_CHARS = 12000    # across a cluster
MIN_ARTICLE_CHARS = 400    # below this, extraction is considered failed

STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "with", "at", "by", "as", "its", "it", "this", "that", "be", "has", "have",
    "will", "can", "could", "may", "might", "after", "over", "under", "from",
    "new", "now", "you", "your", "here", "how", "what", "why", "when", "vs",
}


# ── Feed parsing ─────────────────────────────────────────────────────────────

def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def real_url(link):
    """Google Alerts links are google.com/url?...&url=<real>&... redirects."""
    try:
        p = urlparse(link)
        if p.netloc.endswith("google.com") and p.path == "/url":
            q = parse_qs(p.query)
            if q.get("url"):
                return q["url"][0]
    except Exception:
        pass
    return link


def parse_alert_feed(xml_text):
    """Parse a Google Alerts Atom feed into [{title, url, snippet, published}]."""
    out = []
    root = ET.fromstring(xml_text)
    for entry in root.findall(f"{ATOM}entry"):
        title = strip_tags((entry.findtext(f"{ATOM}title") or ""))
        link_el = entry.find(f"{ATOM}link")
        link = real_url(link_el.get("href")) if link_el is not None else None
        snippet = strip_tags(entry.findtext(f"{ATOM}content") or "")
        published = entry.findtext(f"{ATOM}published") or None
        if title and link:
            out.append({"title": title, "url": link, "snippet": snippet,
                        "published": published})
    return out


# ── Clustering (same story across outlets) ───────────────────────────────────

def title_tokens(title):
    # Drop the trailing " - Outlet Name" most headlines carry.
    t = re.split(r"\s+[-|–]\s+(?=[A-Z][\w .]*$)", title)[0]
    words = re.findall(r"[a-z0-9]+", t.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def similar(a, b):
    """Containment coefficient of title token sets (handles short headlines)."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


SIMILAR_THRESHOLD = 0.5


def cluster_articles(articles):
    """Greedy clustering: an article joins the first cluster whose seed title
    is similar; else starts a new cluster."""
    clusters = []
    for art in articles:
        for cl in clusters:
            if similar(art["title"], cl[0]["title"]) >= SIMILAR_THRESHOLD:
                cl.append(art)
                break
        else:
            clusters.append([art])
    return clusters


# ── Full-article fetch ───────────────────────────────────────────────────────

def fetch_article_text(url):
    """Download the page and extract the main article text (trafilatura)."""
    import trafilatura
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": UA})
        r.raise_for_status()
        text = trafilatura.extract(r.text, include_comments=False,
                                   include_tables=False) or ""
        text = text.strip()
        return text if len(text) >= MIN_ARTICLE_CHARS else None
    except Exception:
        return None


# ── Writing (Claude) ─────────────────────────────────────────────────────────

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "duplicate_of": {
            "type": ["string", "null"],
            "description": "Slug of the existing post if this is the SAME story; else null.",
        },
        "title": {"type": "string"},
        "paragraphs": {"type": "array", "items": {"type": "string"}},
        "image_query": {
            "type": "string",
            "description": "2-4 word stock-photo search for a generic matching image.",
        },
    },
    "required": ["duplicate_of", "title", "paragraphs", "image_query"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are the news writer for WhatPhone, an Indian refurbished-phone price "
    "comparison site. You write short, original news articles about phones and "
    "the phone market for its blog.\n"
    "Rules:\n"
    "- Write a COMPLETELY ORIGINAL article in your own words based on the "
    "source articles provided. Never copy sentences or distinctive phrasing.\n"
    "- 3 to 6 paragraphs, 250-450 words total. Plain text paragraphs, no "
    "markdown, no headings, no links.\n"
    "- Neutral, factual news tone. Lead with the news, then details, then "
    "context. Mention India pricing/availability when the sources cover it.\n"
    "- Title: clear and specific, under 90 characters, no clickbait.\n"
    "- image_query: a 2-4 word stock photo search likely to match generic "
    "photos (e.g. 'samsung smartphone closeup', 'smartphone repair') — never "
    "model numbers that stock sites won't have.\n"
    "- DUPLICATES: you are given the titles+slugs of recently published posts. "
    "If the story you're given is substantially the same story as one of them "
    "(same event, even if from different outlets), set duplicate_of to that "
    "post's slug and leave the other fields minimal. Otherwise duplicate_of "
    "must be null."
)


def write_post(cluster, sources_text, recent_posts):
    """One Claude call: returns dict per WRITER_SCHEMA."""
    import anthropic
    client = anthropic.Anthropic()

    recent = "\n".join(f"- {p['title']} (slug: {p['slug']})" for p in recent_posts) or "(none)"
    src_blocks = []
    for art, text in sources_text:
        src_blocks.append(
            f"SOURCE: {art['source_domain']}\nHEADLINE: {art['title']}\n"
            f"ARTICLE TEXT:\n{text[:MAX_SOURCE_CHARS]}"
        )
    user = (
        f"Recently published posts on our blog:\n{recent}\n\n"
        f"New story, covered by {len(src_blocks)} source(s):\n\n"
        + "\n\n---\n\n".join(src_blocks)
    )[: MAX_TOTAL_CHARS + 2000]

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": WRITER_SCHEMA}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ── Image (Pexels -> R2) ─────────────────────────────────────────────────────

def fetch_image(query, slug):
    """Search Pexels, host the photo on R2 at blog/<slug>.jpg.
    Returns (image_url, credit, credit_url) or (None, None, None)."""
    import os
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return None, None, None
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 5, "orientation": "landscape"},
            headers={"Authorization": key}, timeout=20,
        )
        r.raise_for_status()
        photos = r.json().get("photos") or []
        if not photos:  # retry once with a broader query
            r = requests.get(
                "https://api.pexels.com/v1/search",
                params={"query": "smartphone", "per_page": 5, "orientation": "landscape"},
                headers={"Authorization": key}, timeout=20,
            )
            photos = r.json().get("photos") or []
        if not photos:
            return None, None, None
        p = photos[0]
        src = p["src"].get("large2x") or p["src"].get("large") or p["src"]["original"]
        from db import host_image
        hosted = host_image(src, f"blog/{slug}.jpg")
        return hosted or src, p.get("photographer"), p.get("url")
    except Exception as e:
        log_error(e, stage="pexels")
        return None, None, None


# ── DB helpers ───────────────────────────────────────────────────────────────

def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:80].rstrip("-") or "post"


def unique_slug(supabase, _exec, base):
    slug = base
    n = 2
    while True:
        hit = _exec(lambda: supabase.table("blog_posts").select("id").eq("slug", slug).execute())
        if not hit.data:
            return slug
        slug = f"{base}-{n}"
        n += 1


def build_body_html(paragraphs):
    return "\n".join(f"<p>{html.escape(p.strip())}</p>" for p in paragraphs if p.strip())


# ── Pipeline ─────────────────────────────────────────────────────────────────

def run(dry=False):
    from db import supabase, _exec

    feeds = _exec(lambda: supabase.table("news_feeds").select("url, label").eq("active", True).execute()).data
    if not feeds:
        print("No active feeds in news_feeds — add Google Alerts RSS URLs there.")
        return

    # 1-2. Fetch feeds, collect entries, drop known URLs.
    entries = []
    for f in feeds:
        try:
            r = requests.get(f["url"], timeout=25, headers={"User-Agent": UA})
            r.raise_for_status()
            for e in parse_alert_feed(r.text):
                e["source_domain"] = urlparse(e["url"]).netloc.removeprefix("www.")
                entries.append(e)
        except Exception as e:
            log_error(e, stage="feed", feed=f.get("label") or f["url"])
            print(f"  feed failed: {f.get('label') or f['url']}: {e}")
    # de-dup within the run by URL
    seen = set()
    entries = [e for e in entries if not (e["url"] in seen or seen.add(e["url"]))]
    print(f"{len(entries)} entries from {len(feeds)} feed(s)")
    if not entries:
        return

    known = _exec(lambda: supabase.table("news_articles").select("url")
                  .in_("url", [e["url"] for e in entries]).execute()).data
    known_urls = {k["url"] for k in known}
    fresh = [e for e in entries if e["url"] not in known_urls]
    print(f"{len(fresh)} new (not in news_articles)")
    if not fresh:
        return

    # 3. Cluster same-story coverage.
    clusters = cluster_articles(fresh)
    print(f"{len(clusters)} story cluster(s)")

    # Recent posts (for the duplicate check inside the writer prompt).
    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_POST_DAYS)).isoformat()
    recent_posts = _exec(lambda: supabase.table("blog_posts")
                         .select("id, slug, title, sources")
                         .gte("created_at", since)
                         .order("created_at", desc=True).limit(60).execute()).data

    for cluster in clusters:
        titles = " | ".join(a["title"][:70] for a in cluster[:3])
        try:
            # 4. Full article text — required; never write from snippets alone.
            sources_text = []
            for art in cluster:
                text = fetch_article_text(art["url"])
                if text:
                    sources_text.append((art, text))
                time.sleep(1)
            if not sources_text:
                print(f"  SKIP (no fetchable full text, will retry next run): {titles}")
                continue

            if dry:
                print(f"  CLUSTER ({len(cluster)} src, {len(sources_text)} fetched): {titles}")
                continue

            # 5. Write.
            result = write_post(cluster, sources_text, recent_posts)

            if result.get("duplicate_of"):
                slug = result["duplicate_of"]
                match = next((p for p in recent_posts if p["slug"] == slug), None)
                if match:
                    new_sources = (match.get("sources") or []) + [
                        {"title": a["title"], "url": a["url"], "domain": a["source_domain"]}
                        for a in cluster
                    ]
                    # de-dup sources by url
                    dedup, su = [], set()
                    for s in new_sources:
                        if s["url"] not in su:
                            su.add(s["url"]); dedup.append(s)
                    _exec(lambda: supabase.table("blog_posts").update({
                        "sources": dedup, "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", match["id"]).execute())
                    record_articles(supabase, _exec, cluster, match["id"])
                    print(f"  DUPLICATE of {slug} — sources attached: {titles}")
                else:
                    record_articles(supabase, _exec, cluster, None)
                    print(f"  DUPLICATE (slug {slug} not found) — recorded only: {titles}")
                continue

            paragraphs = [p for p in result.get("paragraphs") or [] if p.strip()]
            if not result.get("title") or len(paragraphs) < 2:
                print(f"  SKIP (writer returned thin content): {titles}")
                record_articles(supabase, _exec, cluster, None)
                continue

            slug = unique_slug(supabase, _exec, slugify(result["title"]))

            # 6. Image.
            image_url, credit, credit_url = fetch_image(result["image_query"], slug)

            # 7. Publish.
            post = _exec(lambda: supabase.table("blog_posts").insert({
                "slug": slug,
                "title": result["title"].strip(),
                "body_html": build_body_html(paragraphs),
                "image_url": image_url,
                "image_credit": credit,
                "image_credit_url": credit_url,
                "sources": [{"title": a["title"], "url": a["url"], "domain": a["source_domain"]}
                            for a in cluster],
            }).execute()).data[0]
            record_articles(supabase, _exec, cluster, post["id"])
            recent_posts.insert(0, {"id": post["id"], "slug": slug,
                                    "title": result["title"], "sources": post["sources"]})
            print(f"  PUBLISHED /blog/{slug}  ({len(cluster)} source(s))")
        except Exception as e:
            log_error(e, stage="cluster", cluster=titles[:120])
            print(f"  cluster failed (will retry next run): {titles}: {e}")


def record_articles(supabase, _exec, cluster, post_id):
    rows = [{"url": a["url"], "title": a["title"], "source_domain": a["source_domain"],
             "snippet": (a.get("snippet") or "")[:500], "published_at": a.get("published"),
             "post_id": post_id} for a in cluster]
    _exec(lambda: supabase.table("news_articles").upsert(rows, on_conflict="url").execute())


if __name__ == "__main__":
    init_sentry("news")
    try:
        run(dry="--dry" in sys.argv)
    except Exception as exc:
        log_error(exc)
        raise
