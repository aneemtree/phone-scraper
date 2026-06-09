"""Shared review helper: pull a product's aggregate rating from its web page.

Most Shopify review apps (Judge.me, Loox, Yotpo, Stamped, ...) inject a
schema.org `aggregateRating` into the product page as JSON-LD (and/or leave
`ratingValue`/`reviewCount` in the markup). We read that — it's per-product, so
it reflects reviews of THAT phone, not a store-wide score.

fetch_aggregate_rating(url) -> (rating: float|None, count: int|None). Returns
(None, None) unless BOTH a rating and a non-zero count are present, so a product
with no genuine reviews is left blank rather than showing a 0/empty rating.
"""
import json
import re
import requests

_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)


def _walk(node):
    """Yield every dict in a nested JSON structure (handles @graph / arrays)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def parse_aggregate_rating(html_text):
    """Extract (rating, count) from a product page's aggregateRating."""
    # 1) Proper JSON-LD blocks.
    for m in _LD.finditer(html_text or ""):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for node in _walk(data):
            agg = node.get("aggregateRating")
            if isinstance(agg, dict):
                rating = _f(agg.get("ratingValue"))
                count = _i(agg.get("reviewCount") or agg.get("ratingCount"))
                if rating and count:
                    return rating, count
    # 2) Fallback: scan the markup around an aggregateRating mention.
    m = re.search(r'aggregateRating.{0,400}', html_text or "", re.S)
    if m:
        seg = m.group(0)
        r = re.search(r'ratingValue"\s*:\s*"?([0-9.]+)', seg)
        c = re.search(r'(?:reviewCount|ratingCount)"\s*:\s*"?([0-9]+)', seg)
        if r and c:
            rating, count = _f(r.group(1)), _i(c.group(1))
            if rating and count:
                return rating, count
    return None, None


def fetch_aggregate_rating(url, session=None, timeout=30):
    """Fetch a product page and return its (rating, count); (None, None) on any
    error so a review lookup never breaks a scrape."""
    try:
        getter = session or requests
        html_text = getter.get(url, timeout=timeout).text
    except Exception:
        return None, None
    return parse_aggregate_rating(html_text)
