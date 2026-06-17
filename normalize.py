"""
Shared helpers for normalizing phone data across all sites.
Keeping these in one place means every scraper produces matching variant_keys,
which is what lets us group the same phone across different stores.
"""
import re

# Common colors to strip from model names. Multi-word ones MUST come first
# so "Phantom Black" is removed before "Black".
COLORS = [
    # Multi-word marketing colors MUST come first so they're removed whole before
    # a single-word rule (e.g. "celestial magic" before "magic" would ever match,
    # and so "magic" is only ever stripped as part of a color phrase — never on
    # its own, which would wrongly eat the Honor *Magic* model line).
    "celestial magic", "celatial magic", "mystic bronze", "mystic green",
    "mystic white", "mystic black", "mystic blue", "mystic silver",
    "phantom black", "phantom white", "deep purple", "space black", "space gray",
    "space grey", "rose gold", "midnight", "starlight", "graphite", "sierra blue",
    "alpine green", "pacific blue", "phantom", "titanium", "black", "white",
    "silver", "gold", "purple", "blue", "green", "red", "pink", "gray", "grey",
    "yellow", "coral", "lavender", "cream", "mint",
    "natural", "natural titanium", "blue titanium", "white titanium",
    "black titanium", "desert titanium", "aurora", "phantom violet",
    "dark matter", "hyperspace",
    "mystic", "celestial", "celatial",
    # Pixel / misc marketing colors that were leaking into model names.
    "porcelain", "obsidian", "hazel", "peony", "aloe", "lemongrass",
    "charcoal", "rose", "snow", "bay", "sage", "lime",
    # Budli leaks — colour qualifiers left over after the base colour is stripped
    # (e.g. "Solar Red" -> "Red" removed -> "Solar"; "Awesome Graphit(e)" ->
    # "Awesome"). Multi-word forms kept first so the bare risky words (bold/legion,
    # which are also non-phone brands elsewhere) are only ever removed as a colour
    # phrase, never on their own.
    "awesome graphite", "awesome graphit", "awesome navy", "awesome lilac",
    "awesome iceblue", "awesome violet", "emerald brown", "sunset orange",
    "legion sky", "bold hold", "navy", "solar", "sierra", "forest", "mist",
    "ash", "chromatic", "meteorite", "cloud", "fluid", "prism", "oxygen",
    "dash", "sunshine", "sunset", "orange", "emerald", "brown", "pearl",
    "peari", "lilac", "iceblue", "graphite", "graphit", "awesome", "mostly",
    "prism cube", "prism crush", "cube", "crush", "lunar", "deepsea", "stormy",
    # Triage leaks (colour qualifiers stores append to the model name, blocking
    # the GSMArena match + cross-store merge). Multi-word first.
    "deep ocean", "marshmallow", "atlantis", "sapphire", "stardust", "glacier", "cross",
    # Marketing colour QUALIFIERS left after the base colour is stripped:
    # Samsung F62 "Laser Grey/Green" -> "Laser"; Pixel 9a "Iris"; Samsung M52
    # "Icy Blue" -> "Icy"/"Ice". None are real model names, so safe to strip.
    "icy blue", "laser", "iris", "icy", "ice",
]

# Roman numerals (II-XII) that title-casing would lower-case (e.g. Sony "Xperia 1
# III" -> "Iii"); uppercased back in clean_model. Single "I" is excluded (too
# ambiguous); "V"/"X" are included for Xperia "1 V"/iPhone "X".
ROMAN_NUMERALS = {"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}


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
    # "Refurbished" is too vague to compare across stores (it's the default label
    # for ungraded stock), so it's recorded as "Unknown Condition" everywhere.
    if c == "Refurbished":
        return "Unknown Condition"
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
    ("redmi", "Xiaomi Redmi"),
    ("poco", "POCO"),
    ("pixel", "Google Pixel"),
    ("narzo", "Realme Narzo"),
    ("nord", "OnePlus Nord"),
    ("cmf", "Nothing CMF"),
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
    # Preserve parenthesised model identifiers BEFORE the generic paren strip below:
    # Nothing/CMF "Phone (1)"/"(2a)" and iPhone "SE (2020)" carry the model number
    # in parens, which the strip would otherwise delete (collapsing Phone 1/2/3 and
    # SE 2020/2022). For iPhone SE, also map generation ordinals to the year, since
    # stores mix "SE 2022" / "SE 3rd Gen" / "SE (3rd generation)" for the same phone.
    if re.search(r"\biphone\s+se\b", t, re.I):
        t = re.sub(r"\b1st\s+gen\w*", "2016", t, flags=re.I)
        t = re.sub(r"\b2nd\s+gen\w*", "2020", t, flags=re.I)
        t = re.sub(r"\b3rd\s+gen\w*", "2022", t, flags=re.I)
        t = re.sub(r"\bSE\s*\(\s*(20\d{2})\s*\)", r"SE \1", t, flags=re.I)
    t = re.sub(r"\bphone\s*\(\s*(\d+[a-z]?)\s*\)", r"Phone \1", t, flags=re.I)
    t = re.sub(r"\(.*?\)", " ", t)                      # remove (...) groups
    t = re.sub(r"\s*/\s*", " ", t)                       # "128GB/256GB" -> tokens
    # "+" is a model suffix (Realme 12+, Galaxy S21+) — turn it into the word
    # "Plus" so it survives make_variant_key (which strips non-alphanumerics) and
    # stays distinct from the non-plus model. The (?!\d) guard avoids touching
    # RAM+storage notation like "8+256".
    t = re.sub(r"\+(?!\d)", " Plus", t)
    # ControlZ condition grades ("Premium Renewed", "Saver Series") can lead or
    # trail the title/slug. Strip the grade words so they never leak into the
    # model name. The old "saver series.*$" rule deleted everything after it —
    # including the model itself when the grade came first ("Saver Series iPhone
    # 12" -> ""). "renewed" is removed by the refurb pass below; we strip the
    # remaining "premium"/"saver"/"series" grade words here.
    t = re.sub(r"\b(premium[\s_-]+renewed|saver[\s_-]+series|premium|saver|series)\b", " ", t, flags=re.I)
    t = re.sub(r"\b\d+\s?(GB|TB)\b", " ", t, flags=re.I) # storage
    # Stray "RAM" label with no number (the number was already stripped as storage
    # above, e.g. "Note 20 RAM , Mystic"). Remove the orphaned keyword + any comma
    # left dangling so it never leaks into the model name.
    t = re.sub(r"\bram\b", " ", t, flags=re.I)
    t = re.sub(r"\bsim\s*slot\b", " ", t, flags=re.I)    # "iPhone 15 Pro Sim Slot" leak
    for c in COLORS:                                     # colors (longest first)
        t = re.sub(rf"\b{re.escape(c)}\b", " ", t, flags=re.I)
    t = re.sub(r"\s*,\s*", " ", t)                       # drop orphaned commas
    t = re.sub(r"\b(refurbished|renewed|pre-?owned|pre-?loved|used|open\s*box|certified|certified refurbished)\b", " ", t, flags=re.I)
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

    # Collapse whitespace BEFORE the anchored brand pass. Stripping noise words
    # (refurbished/renewed/grade labels) leaves leading/double spaces, and the
    # "^token\s" anchor below would otherwise miss (" iPhone 12" never matches
    # "^iphone\s"), leaving the model un-prefixed and splitting the brand filter.
    # Also turn slug-style separators between lowercase/digit chars into spaces so
    # a slug fallback ("premium-renewed-iphone-12") reaches the brand pass too;
    # the lookarounds skip uppercase model numbers like "SM-G991B".
    t = re.sub(r"(?<=[a-z0-9])[-_](?=[a-z0-9])", " ", t)
    t = re.sub(r"^[\s\-_–|]+|[\s\-_–|]+$", "", t)  # trim leading/trailing separators
    t = re.sub(r"\s+", " ", t).strip()

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
    # "Mi" before "Redmi" is a redundant double sub-brand ("Xiaomi Mi Redmi K20"
    # / "Mi Redmi Note 10"). Redmi is its own line — drop the stray "Mi" so it
    # collapses to "Xiaomi Redmi ..." and matches the other stores' key.
    t = re.sub(r"\bMi\s+(?=Redmi\b)", "", t, flags=re.I)
    # Doubled "Mi Mi" (e.g. "Xiaomi Mi Mi 5") -> single "Mi".
    t = re.sub(r"\bMi\s+Mi\b", "Mi", t, flags=re.I)
    # Xiaomi dropped the "Mi" brand from the 12-series on ("Mi 14" -> "Xiaomi 14");
    # "Mi 11"/older keep it. Drop "Mi" only before a 12-19 flagship number.
    t = re.sub(r"\bMi\s+(1[2-9]\b)", r"\1", t, flags=re.I)
    # "Mi 11 Lite"/"Mi 11 Lite NE": GSMArena/Beebom drop "Mi" here (vanilla "Mi 11"
    # keeps it), so drop "Mi" only before "11 Lite".
    t = re.sub(r"\bMi\s+(?=11\s+Lite\b)", "", t, flags=re.I)
    # "iPhone 17 Air" and "iPhone Air" are the same phone; canonicalize to "iPhone Air".
    t = re.sub(r"\biPhone\s+17\s+Air\b", "iPhone Air", t, flags=re.I)
    # "Vivo iQOO …" — iQOO is a standalone brand chip (other stores list it bare),
    # so drop the redundant leading "Vivo" parent to share one key.
    t = re.sub(r"\bVivo\s+(?=iQOO\b)", "", t, flags=re.I)
    # CMF is Nothing's sub-brand. Stores write "CMF by Nothing Phone 2 Pro"; collapse
    # the connector so the sub-brand prefix below yields "Nothing CMF Phone 2 Pro".
    t = re.sub(r"\bcmf\s+by\s+nothing\b", "CMF", t, flags=re.I)
    t = re.sub(r"\bnothing\s+cmf\s+by\s+nothing\b", "Nothing CMF", t, flags=re.I)
    # "Xiaomi POCO …" — POCO is its own chip (other stores list it bare). Some
    # stores file POCO under the Xiaomi brand (e.g. gadgetrebirth), which prepends
    # "Xiaomi"; drop it so "POCO F6 Pro" shares one key cross-store.
    t = re.sub(r"\bXiaomi\s+(?=POCO\b)", "", t, flags=re.I)
    # Motorola: stores mix "Moto X", "Motorola Moto X" and "Motorola X" for the same
    # phone. Collapse the redundant "Moto" so all become "Motorola X" (one card).
    t = re.sub(r"\bmotorola\s+moto\b", "Motorola", t, flags=re.I)
    t = re.sub(r"\bmoto\b", "Motorola", t, flags=re.I)
    # Samsung's Galaxy lines are sometimes listed WITHOUT the "Galaxy" word
    # (itradeit: "Samsung S25 Ultra", "Samsung Note 20"). Insert it so the key
    # matches the stores that include it ("Samsung Galaxy S25 Ultra"). Only when
    # "Galaxy" is absent AND the next token is a Galaxy-series marker (S/A/M/F/J +
    # digit, or Note/Z/Fold/Flip/Tab) so non-Galaxy Samsung products are untouched.
    if re.search(r"\bSamsung\b", t, re.I) and not re.search(r"\bGalaxy\b", t, re.I):
        t = re.sub(r"\bSamsung\s+(?=(?:[SAMFJ]\d)|(?:Note|Z|Fold|Flip|Tab)\b)",
                   "Samsung Galaxy ", t, count=1, flags=re.I)
    t = re.sub(r"\bunbox(?:ed)?\b", " ", t, flags=re.I)  # strip unboxed/unbox
    t = re.sub(r"[/\\|]+$", "", t).strip()  # strip trailing slashes/pipes
    t = re.sub(r"\b(controlz|cashify|refit|xtracover|croma)\b", " ", t, flags=re.I)
    t = re.sub(r"\b(special series|saver series|aurora|titanium|esim|e-?sim|physical sim|single sim|dual sim|dual)\b", " ", t, flags=re.I)
    # Spacing: GSMArena and some stores write "Z Fold4"/"Reno15"/"Nord CE3" while
    # others use a space; unify to the spaced form so the same phone shares one key.
    t = re.sub(r"\b(fold|flip|reno)(\d)", r"\1 \2", t, flags=re.I)
    t = re.sub(r"\bce(\d)", r"CE \1", t, flags=re.I)
    # Network/connectivity suffixes (5G, 4G, LTE, WiFi variants)
    t = re.sub(r"\b(5g|4g|lte|3g|wifi|wi-fi)\b", " ", t, flags=re.I)
    # Regional/market variants
    t = re.sub(r"\b(india|indian|global|international|export|us|usa|uk|eu)\b", " ", t, flags=re.I)
    # Packaging/condition noise
    t = re.sub(r"\b(with\s+box|without\s+box|brand\s+box|original\s+box|sealed\s+box|open\s+box)\b", " ", t, flags=re.I)
    t = re.sub(r"\b(accessories|charger|cable|earphone|adapter)\b", " ", t, flags=re.I)
    # Model number noise (e.g. "SM-G991B", "CPH2197")
    t = re.sub(r"\b[A-Z]{2,4}-?[A-Z0-9]{4,}\b", " ", t)
    # Year suffixes standalone (e.g. "iPhone 13 2021" -> "iPhone 13"), EXCEPT when
    # preceded by "SE " — the year distinguishes the SE generations (2016/2020/2022).
    t = re.sub(r"(?<![sS][eE] )\b(20[12][0-9])\b", " ", t)
    # "Series" standalone
    t = re.sub(r"\bseries\b", " ", t, flags=re.I)
    # Marketing "Edition" suffix (Realme "GT Master Edition" -> "GT Master", Galaxy
    # "M21 Edition" -> "M21). Subset matching tolerates it where GSMArena keeps it.
    t = re.sub(r"\bedition\b", " ", t, flags=re.I)
    # Stray "Storage" label left by comma-style titles ("… 128GB Storage")
    t = re.sub(r"\bstorage\b", " ", t, flags=re.I)
    # Marketing tokens some stores append ("Galaxy S25 Ultra AI New", "Z Fold5
    # AI") — strip standalone "AI"/"New" so they don't fragment the model from
    # the same phone on other stores. No real phone model is the bare word
    # "AI"/"New" (Galaxy "AI" branding is marketing; "renewed" lacks the boundary).
    t = re.sub(r"\b(ai|new)\b", " ", t, flags=re.I)
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
    # Uppercase the FE ("Fan Edition") suffix regardless of how a store cased it
    # ("S20 Fe" -> "S20 FE") so it matches the same phone elsewhere. The
    # title-casing above lower-cases mixed-case "Fe"; this restores it.
    t = re.sub(r"\bfe\b", "FE", t, flags=re.I)
    # iPhone X-series suffixes: normalize "Xs"/"Xr" casing to "XS"/"XR".
    t = re.sub(r"\bxs\b", "XS", t, flags=re.I)
    t = re.sub(r"\bxr\b", "XR", t, flags=re.I)
    t = re.sub(r"\bgt\b", "GT", t, flags=re.I)
    t = " ".join(w.upper() if w.lower() in ROMAN_NUMERALS else w for w in t.split())
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