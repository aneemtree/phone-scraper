"""
Shared helpers for normalizing phone data across all sites.
Keeping these in one place means every scraper produces matching variant_keys,
which is what lets us group the same phone across different stores.
"""
import re

# Common colors to strip from model names. Multi-word ones MUST come first
# so "Phantom Black" is removed before "Black".
COLORS = [
    "phantom black", "phantom white", "deep purple", "space black", "space gray",
    "space grey", "rose gold", "midnight", "starlight", "graphite", "sierra blue",
    "alpine green", "pacific blue", "phantom", "titanium", "black", "white",
    "silver", "gold", "purple", "blue", "green", "red", "pink", "gray", "grey",
    "yellow", "coral", "lavender", "cream", "mint",
    "natural", "natural titanium", "blue titanium", "white titanium",
    "black titanium", "desert titanium", "aurora", "phantom violet",
]


def normalize_storage(raw: str | None) -> str | None:
    """'256-GB', '256 GB', '1-TB' -> '256GB' / '1TB'."""
    if not raw:
        return None
    s = raw.upper().replace("-", "").replace(" ", "")
    m = re.search(r"(\d+)(GB|TB)", s)
    return f"{m.group(1)}{m.group(2)}" if m else None


def normalize_ram(raw: str | None) -> str | None:
    """Pulls a RAM value like '8GB' ONLY when explicitly labelled as RAM.
    Storage figures (e.g. '128GB' in a title) must NOT be read as RAM, so we
    require the word 'RAM' near the number. Returns None otherwise."""
    if not raw:
        return None
    s = raw.upper()
    # Match "8GB RAM", "8 GB RAM", or "RAM: 8GB"
    m = re.search(r"(\d+)\s?GB\s?RAM\b", s) or re.search(r"RAM[:\s]+(\d+)\s?GB", s)
    return f"{m.group(1)}GB" if m else None


def clean_model(title: str) -> str:
    """Strip storage, color, and refurb noise to get a clean model name."""
    t = title
    t = re.sub(r"\(.*?\)", " ", t)                      # remove (...) groups
    t = re.sub(r"saver series.*$", " ", t, flags=re.I)  # ControlZ saver suffix
    t = re.sub(r"\b\d+\s?(GB|TB)\b", " ", t, flags=re.I) # storage
    for c in COLORS:                                     # colors (longest first)
        t = re.sub(rf"\b{re.escape(c)}\b", " ", t, flags=re.I)
    t = re.sub(r"\b(refurbished|renewed|pre-?owned|open box|certified)\b", " ", t, flags=re.I)
    t = re.sub(r"\b(special series|saver series|aurora|titanium|esim|e-sim|physical sim|dual sim|5g)\b", " ", t, flags=re.I)
    t = re.sub(r"[\-–|()]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Normalize common casing: "Iphone"/"iphone" -> "iPhone"
    t = re.sub(r"\biphone\b", "iPhone", t, flags=re.I)
    t = re.sub(r"\bipad\b", "iPad", t, flags=re.I)
    # Title-case the rest while preserving iPhone/iPad
    words = []
    for w in t.split():
        if w in ("iPhone", "iPad"):
            words.append(w)
        elif w.isupper() and len(w) > 1:
            words.append(w.capitalize())
        else:
            words.append(w[:1].upper() + w[1:] if w else w)
    t = " ".join(words)
    return t


def make_variant_key(model: str, storage: str | None, ram: str | None) -> str:
    """Cross-site grouping key. Color-free; RAM optional (blank for iPhones).
    Uses URL-safe separators (no pipes) so keys work cleanly in page URLs."""
    base = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    parts = [base]
    if storage: parts.append(storage.lower())
    if ram: parts.append(ram.lower())
    return "_".join(parts)