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


def parse_size_string(size_str: str | None) -> tuple[str | None, str | None]:
    """Parse a size string into (ram, storage) for any scraper.

    Handles all known formats across stores:
      "6GB|1TB"           → ram=6GB,  storage=1TB   (Refit pipe format)
      "4GB|128GB"         → ram=4GB,  storage=128GB
      "6 GB RAM / 128 GB" → ram=6GB,  storage=128GB  (Cashify explicit RAM)
      "4 GB / 256 GB"     → ram=4GB,  storage=256GB  (Cashify slash format)
      "8 GB / 1 TB"       → ram=8GB,  storage=1TB
      "128GB"             → ram=None, storage=128GB  (single value)
      "1TB"               → ram=None, storage=1TB

    Rules:
    - Explicit "RAM" keyword → that value is RAM
    - Two values: smaller = RAM (converted to GB for comparison), larger = storage
    - RAM sanity check: if "smaller" > 32GB, both are storage variants, not RAM
    - Single value: always storage
    """
    if not size_str:
        return None, None

    def _to_gb(part: str) -> int | None:
        """Convert a size token to GB integer for comparison."""
        part = part.strip().upper().replace(" ", "").replace("-", "")
        m = re.search(r"(\d+(?:\.\d+)?)TB", part)
        if m:
            return int(float(m.group(1)) * 1024)
        m = re.search(r"(\d+)GB", part)
        if m:
            return int(m.group(1))
        return None

    # Explicit RAM label (e.g. "6 GB RAM / 128 GB" or "6GB RAM")
    ram_label = re.search(r"(\d+)\s*GB\s*RAM", size_str, re.I)
    if ram_label:
        ram = f"{ram_label.group(1)}GB"
        # Storage is whatever GB/TB value remains after removing the RAM part
        rest = re.sub(r"\d+\s*GB\s*RAM", "", size_str, flags=re.I)
        storage = normalize_storage(rest.strip(" /|-"))
        return ram, storage

    # Split on common separators: pipe, slash, comma
    parts = [p.strip() for p in re.split(r"[|/,]", size_str) if p.strip()]
    if len(parts) == 2:
        va, vb = _to_gb(parts[0]), _to_gb(parts[1])
        la, lb = normalize_storage(parts[0]), normalize_storage(parts[1])
        if va is not None and vb is not None:
            smaller_gb = min(va, vb)
            # Sanity: RAM is never > 32GB in any phone
            if smaller_gb > 32:
                # Both are storage sizes (e.g. upgrade options) — no RAM
                return None, la if va <= vb else lb
            ram_label_, storage_label = (la, lb) if va <= vb else (lb, la)
            return ram_label_, storage_label
        # Only one side parsed — return as storage
        return None, la or lb

    # Single value: storage only
    return None, normalize_storage(size_str.strip())


def normalize_condition(condition: str | None) -> str | None:
    """Normalize condition names to consistent title case."""
    if not condition:
        return None
    # Strip and title case
    c = condition.strip().title()
    # Fix known variations
    c = re.sub(r"\bRenewed\b", "Renewed", c)
    c = re.sub(r"\bRefurbished\b", "Refurbished", c)
    c = re.sub(r"\bSuperb\b", "Superb", c)
    return c


# Keywords that indicate a non-phone product
NON_PHONE_KEYWORDS = [
    "power bank", "powerbank", "power-bank",
    "photography kit", "photo kit", "kit",
    "smartwatch", "smart watch", "watch",
    "tablet", "ipad",
    "laptop", "notebook",
    "earphone", "earbuds", "headphone", "airpods",
    "charger", "cable", "adapter", "hub",
    "case", "cover", "screen guard", "tempered glass",
    "accessory", "accessories",
    "stand", "holder", "mount",
    "speaker", "camera",
    "legend edition",  # photography kit variant
]

PHONE_BRANDS = [
    "iphone", "samsung", "galaxy", "oneplus", "oppo", "vivo", "realme",
    "xiaomi", "redmi", "poco", "iqoo", "motorola", "nokia", "google",
    "pixel", "nothing", "asus", "lg", "huawei", "honor", "sony",
    "infinix", "tecno", "lava", "micromax", "mi",
]

# Maps a leading sub-brand/alias token (matched case-insensitively at the START
# of the model name) to its canonical "Parent Sub" prefix. This guarantees the
# brand filter never splits — e.g. "Pixel 7", "GOOGLE PIXEL 7" and "Google Pixel
# 7" all collapse to "Google Pixel 7" so they share the single "Google" chip
# instead of fragmenting into "Pixel" + "Google". Order matters: longer/more
# specific tokens must come before shorter ones (e.g. "redmi" before "mi").
SUB_BRAND_PREFIX = [
    ("iphone", "Apple iPhone"),
    ("ipad", "Apple iPad"),
    ("galaxy", "Samsung Galaxy"),
    ("moto", "Motorola Moto"),
    ("redmi", "Xiaomi Redmi"),
    ("poco", "POCO"),
    ("pixel", "Google Pixel"),
    ("narzo", "Realme Narzo"),
    ("nord", "OnePlus Nord"),
    ("mi", "Xiaomi Mi"),
]

# Canonical casing for standalone parent-brand words, applied as a final pass so
# the brand chip reads consistently regardless of how a store cased the title.
BRAND_CASE = {
    "apple": "Apple", "samsung": "Samsung", "oneplus": "OnePlus",
    "oppo": "OPPO", "vivo": "Vivo", "realme": "Realme", "xiaomi": "Xiaomi",
    "poco": "POCO", "iqoo": "iQOO", "motorola": "Motorola", "nokia": "Nokia",
    "google": "Google", "nothing": "Nothing", "asus": "ASUS", "lg": "LG",
    "huawei": "Huawei", "honor": "Honor", "sony": "Sony", "infinix": "Infinix",
    "tecno": "Tecno", "lava": "Lava", "micromax": "Micromax",
    "iphone": "iPhone", "ipad": "iPad",
}

def is_phone(name: str, slug: str = "") -> bool:
    """Return True if the product name/slug looks like a phone, False if accessory/non-phone."""
    text = (name + " " + slug).lower()
    # Reject if any non-phone keyword found
    for kw in NON_PHONE_KEYWORDS:
        if kw in text:
            return False
    return True


def parse_name_from_listing(raw: str, href: str = "") -> tuple[str, str | None, str | None]:
    """Parse model, ram, storage from a listing card product name.
    Handles all known formats across stores:
      "Apple iPhone 11 (64 GB) Black"              → iPhone 11, None, 64GB
      "Apple iPhone 11 (64 GB, Matte Space Grey)"  → iPhone 11, None, 64GB
      "Redmi Note 12 (8GB RAM, 128GB)"              → Note 12, 8GB, 128GB
      "Samsung Galaxy S23 FE 8/128GB"               → S23 FE, 8GB, 128GB
      "Apple iPhone 13 Pro 512GB"                   → 13 Pro, None, 512GB
    Falls back to URL slug if name has no storage.
    """
    import re as _re

    raw = raw.strip()
    ram, storage = None, None

    # 1. Parenthesised storage: (64 GB), (128GB), (64 GB, Color), (8GB RAM, 128GB)
    paren = _re.search(r"\(([^)]+)\)", raw)
    if paren:
        content = paren.group(1)
        ram, storage = parse_size_string(content)
        if not storage:
            m = _re.search(r"(\d+\s*(?:GB|TB))", content, _re.I)
            if m:
                storage = normalize_storage(m.group(1))
        name_part = raw[:paren.start()].strip()

        # If paren had no storage (e.g. "(Fold 7)"), check for trailing size after paren
        if not storage:
            after_paren = raw[paren.end():].strip()
            slash = _re.search(r"^.*?(\d+)/(\d+\s*(?:GB|TB))\s*$", after_paren, _re.I)
            if slash:
                ram = normalize_storage(slash.group(1) + "GB")
                storage = normalize_storage(slash.group(2))
            else:
                size_m = _re.search(r"(\d+\s*(?:GB|TB))\s*$", after_paren, _re.I)
                if size_m:
                    storage = normalize_storage(size_m.group(1))
    else:
        name_part = raw

        # 2. Slash format at end: "8/128GB", "12/256GB"
        slash = _re.search(r"\s+(\d+)/(\d+\s*(?:GB|TB))\s*$", raw, _re.I)
        if slash:
            ram = normalize_storage(slash.group(1) + "GB")
            storage = normalize_storage(slash.group(2))
            name_part = raw[:slash.start()].strip()
        else:
            # 3. Space-separated tokens at end: "8GB 256GB", "512GB"
            tokens = raw.split()
            size_tokens = []
            remaining = list(tokens)
            while remaining:
                last = remaining[-1]
                if _re.match(r"^\d+\s*(?:GB|TB)$", last, _re.I):
                    size_tokens.insert(0, last)
                    remaining.pop()
                else:
                    break
            if size_tokens:
                name_part = " ".join(remaining)
                if len(size_tokens) == 1:
                    storage = normalize_storage(size_tokens[0])
                else:
                    ram, storage = parse_size_string("|".join(size_tokens))

    # 4. URL slug fallback: "64-gb", "128-gb", "256gb"
    if not storage and href:
        slug_m = _re.search(r"[-_](\d+)[-_]?(gb|tb)", href, _re.I)
        if slug_m:
            unit = "TB" if slug_m.group(2).lower() == "tb" else "GB"
            storage = normalize_storage(f"{slug_m.group(1)}{unit}")

    # Strip trailing noise: "- Refurbished", color words
    name_part = _re.sub(r"\s*[-–]\s*Refurbished.*$", "", name_part, flags=_re.I).strip()
    model = clean_model(name_part)
    return model, ram, storage


def clean_model(title: str) -> str:
    """Strip storage, color, and refurb noise to get a clean model name."""
    t = title
    t = re.sub(r"\(.*?\)", " ", t)                      # remove (...) groups
    t = re.sub(r"saver series.*$", " ", t, flags=re.I)  # ControlZ saver suffix
    t = re.sub(r"\b\d+\s?(GB|TB)\b", " ", t, flags=re.I) # storage
    for c in COLORS:                                     # colors (longest first)
        t = re.sub(rf"\b{re.escape(c)}\b", " ", t, flags=re.I)
    t = re.sub(r"\b(refurbished|renewed|pre-?owned|open\s*box|certified|certified refurbished)\b", " ", t, flags=re.I)
    t = re.sub(r"^buy\s+", "", t, flags=re.I)  # strip leading "Buy " prefix

    # Normalize iPhone/iPad casing EARLY so later passes (model-number stripping,
    # brand prefixing) see a canonical "iPhone"/"iPad" token. Without this, an
    # uppercase "IPHONE" gets eaten by the model-number noise regex below and a
    # lowercase "iphone" misses the brand-prefix step, producing a stray "iPhone"
    # brand that splits the Apple filter into "Apple" + "iPhone".
    t = re.sub(r"\biphone\b", "iPhone", t, flags=re.I)
    t = re.sub(r"\bipad\b", "iPad", t, flags=re.I)

    # Canonicalize brand-word casing EARLY too. An all-caps brand like "GOOGLE"
    # or sub-brand like "GALAXY"/"REDMI" would otherwise be deleted by the
    # model-number noise regex below; cased to mixed-case it's safe and stays in
    # the model name (so "SAMSUNG GALAXY S23" keeps "Galaxy" and matches the key
    # "samsung-galaxy-s23" from other stores).
    for token, cased in BRAND_CASE.items():
        t = re.sub(rf"\b{token}\b", cased, t, flags=re.I)
    for _token, _prefix in SUB_BRAND_PREFIX:
        _word = _prefix.split(" ")[-1]  # canonical-cased sub-brand word, e.g. "Galaxy"
        t = re.sub(rf"\b{re.escape(_word)}\b", _word, t, flags=re.I)

    # Normalize brand names. For each sub-brand/alias, anchor to the START and
    # match case-insensitively so every variant collapses to one canonical brand
    # prefix (one filter per brand). The "parent already present" guard skips the
    # rewrite when the canonical parent is already the leading word, preventing
    # doubles like "Samsung Samsung Galaxy" or "Xiaomi Xiaomi Redmi".
    for token, prefix in SUB_BRAND_PREFIX:
        parent = prefix.split(" ", 1)[0]  # e.g. "Samsung" from "Samsung Galaxy"
        if re.match(rf"^{re.escape(parent)}\s", t, flags=re.I):
            continue  # already starts with the canonical parent brand
        t = re.sub(rf"^{token}\s", prefix + " ", t, flags=re.I)
    t = re.sub(r"\bunbox(?:ed)?\b", " ", t, flags=re.I)  # strip unboxed/unbox
    t = re.sub(r"[/\\|]+$", "", t).strip()  # strip trailing slashes/pipes
    t = re.sub(r"\b(controlz|cashify|refit|xtracover|croma)\b", " ", t, flags=re.I)
    t = re.sub(r"\b(special series|saver series|aurora|titanium|esim|e-?sim|physical sim|dual sim)\b", " ", t, flags=re.I)
    # Network/connectivity suffixes (5G, 4G, LTE, WiFi variants)
    t = re.sub(r"\b(5g|4g|lte|3g|wifi|wi-fi)\b", " ", t, flags=re.I)
    # Regional/market variants
    t = re.sub(r"\b(india|indian|global|international|export|us|usa|uk|eu)\b", " ", t, flags=re.I)
    # Packaging/condition noise
    t = re.sub(r"\b(with\s+box|without\s+box|brand\s+box|original\s+box|sealed\s+box|open\s+box)\b", " ", t, flags=re.I)
    t = re.sub(r"\b(accessories|charger|cable|earphone|adapter)\b", " ", t, flags=re.I)
    # Model number noise (e.g. "SM-G991B", "CPH2197")
    t = re.sub(r"\b[A-Z]{2,4}-?[A-Z0-9]{4,}\b", " ", t)
    # Year suffixes standalone (e.g. "iPhone 13 2021" -> "iPhone 13")
    t = re.sub(r"\b(20[12][0-9])\b", " ", t)
    # "Series" standalone
    t = re.sub(r"\bseries\b", " ", t, flags=re.I)
    t = re.sub(r"[\-–|()]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Normalize common casing: "Iphone"/"iphone" -> "iPhone"
    t = re.sub(r"\biphone\b", "iPhone", t, flags=re.I)
    t = re.sub(r"\bipad\b", "iPad", t, flags=re.I)
    # Title-case words while preserving:
    # - Known brand tokens (iPhone, iPad)
    # - Alphanumeric tokens like "2T", "S23", "FE", "CE" (all caps short = keep caps)
    words = []
    for w in t.split():
        if w in ("iPhone", "iPad"):
            words.append(w)
        elif re.match(r'^[A-Z0-9]+$', w) and len(w) <= 4:
            # Short all-caps/alphanumeric token like FE, CE, 2T, 5G — keep as-is
            words.append(w)
        elif re.match(r'^[0-9]+[A-Z]+$', w):
            # Numeric+uppercase like 2T, 5G — keep as-is
            words.append(w)
        else:
            words.append(w[:1].upper() + w[1:].lower() if w else w)
    t = " ".join(words)
    # Apply canonical brand casing AFTER title-casing so brand words aren't
    # overwritten (e.g. "Poco" -> "POCO", "Iqoo" -> "iQOO", "Oppo" -> "OPPO").
    for token, cased in BRAND_CASE.items():
        t = re.sub(rf"\b{token}\b", cased, t, flags=re.I)
    return t


def make_variant_key(model: str, storage: str | None, ram: str | None = None) -> str:
    """Cross-site grouping key. Uses model + storage ONLY (no RAM).
    RAM is stored in the ram column for display but excluded from the key
    because most stores don't surface RAM, causing cross-store mismatches.
    Uses URL-safe separators so keys work cleanly in page URLs."""
    base = re.sub(r"[^a-z0-9]+", "-", (model or "").lower()).strip("-")
    parts = [base]
    if storage:
        parts.append(re.sub(r"[^a-z0-9]+", "", storage.lower()))
    return "_".join(parts)


# Keyword → semantic role for matching Shopify variant option names. Shopify
# stores variant attributes in option1/2/3, but the *position* of each attribute
# varies per store/product. Mapping by the option's NAME (from prod["options"])
# instead of a hardcoded position prevents storage/grade getting read from the
# wrong slot — which otherwise collapses every storage into one variant_key.
_OPTION_ROLE_KEYWORDS = {
    "grade": ("grade", "condition", "quality"),
    "size": ("storage", "size", "memory", "rom", "capacity", "variant", "ram"),
    "color": ("color", "colour"),
}


def shopify_option_index(product: dict) -> dict:
    """Return {role: position} mapping for a Shopify product's options.

    role is one of "grade" | "size" | "color"; position is the 1-based index
    used to read v["option{position}"]. Roles with no matching option name are
    omitted, so callers should fall back to their known default positions.
    """
    idx = {}
    for opt in product.get("options", []) or []:
        name = (opt.get("name") or "").strip().lower()
        pos = opt.get("position")
        if not name or not pos:
            continue
        for role, keywords in _OPTION_ROLE_KEYWORDS.items():
            if role in idx:
                continue
            if any(k in name for k in keywords):
                idx[role] = pos
                break
    return idx