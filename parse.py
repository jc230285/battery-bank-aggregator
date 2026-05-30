"""Pure parsing helpers (no Playwright) so they can be unit-tested in isolation."""
import re

WIRELESS_KEYS = ["wireless charg", "qi charg", "qi-certified", "qi certified",
                 "qi2", "magsafe", "induction charg", "wireless power"]
DISPLAY_KEYS = ["lcd", "digital display", "display screen", "led display",
                "smart display", "percentage display", "screen display",
                "battery level display", "battery level indicator",
                "charge indicator display", "led indicator", "power indicator",
                "oled display", "oled screen", "status indicator",
                "charge level indicator", "remaining capacity indicator",
                "power display", "charging indicator"]
PASSTHROUGH_KEYS = ["pass-through", "pass through", "passthrough",
                    "simultaneous charging", "charge while charging",
                    "charge through", "charge and be charged",
                    "use while charging", "while charging the",
                    "while being charged", "recharge while discharging",
                    "charge and discharge simultaneously",
                    "while charging"]
SOLAR_KEYS = ["solar", "photovoltaic", "mppt", "pv input", "pv charging"]


def asin_from_url(url):
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url or "")
    return m.group(1) if m else None


# Relevance: keep actual power banks, drop accessories (cases, cables, covers...).
POSITIVE_KEYS = [
    "power bank", "powerbank", "battery bank", "portable charger",
    "power pack", "charging bank", "portable power bank",
    "portable battery", "backup battery", "external battery",
]
# Definitive accessory signals: the product itself IS a case/protector, or is
# explicitly "for" another device. These win even if "power bank" appears
# (e.g. "Hard Travel Case for Anker Power Bank"). Kept deliberately narrow so
# real power banks that merely bundle a cable/adapter are NOT excluded.
STRONG_EXCLUDE = [
    "case for", "case only", "case replacement", "replacement for",
    "replacement battery", "carrying case", "travel case", "cover for",
    "sleeve for", "pouch for", "bag for", "screen protector", "tempered glass",
]
# Weaker signals: only disqualify when NO positive power-bank phrase is present
# (so a standalone cable/charger/holder is dropped, but a power bank that
# mentions an included cable is kept).
WEAK_EXCLUDE = [
    " cable", "adapter", "adaptor", "wall charger", "mains charger",
    "stand for", "holder", "lanyard", "earbud", "headphone", "power strip",
    "extension lead", "aa batter", "aaa batter", "car battery", "9v battery",
    "button cell", " case", "cover", " dock", "mount for",
]


def is_accessory(title):
    """True only for *definitive* accessories (case/cover/protector "for" a
    device). Used for safe deletion of stored rows — never deletes a borderline
    item, unlike the weak-exclude path in is_battery_bank."""
    t = (title or "").lower()
    return bool(t) and any(k in t for k in STRONG_EXCLUDE)


def is_battery_bank(title, mah=None):
    """True if the listing looks like a power bank rather than an accessory."""
    t = (title or "").lower()
    if not t:
        return mah is not None  # no title: trust a parsed capacity
    if any(k in t for k in STRONG_EXCLUDE):
        return False
    if any(k in t for k in POSITIVE_KEYS):
        return True
    if any(k in t for k in WEAK_EXCLUDE):
        return False
    # A capacity figure in the title is a strong implicit positive signal —
    # standalone mAh numbers only appear on battery products.
    if mah is not None or extract_mah(title) is not None:
        return True
    return False


def extract_mah(text):
    """Largest plausible mAh figure in the text, else None.

    Handles both mAh and bare Ah (some listings use '10Ah' for a 10000 mAh bank).
    Bare-Ah values are only used when no mAh figures are found, and only when
    the value×1000 falls in the plausible range."""
    if not text:
        return None
    t = text.lower().replace(",", "")
    # European thousands separator: "40.000mAh" means 40,000 mAh.
    # Pattern: 1-4 digits, a period, exactly 3 digits, directly before mAh.
    t = re.sub(r"(\d{1,4})\.(\d{3})(?=\s*m\s*a\s*h)", lambda m: m.group(1) + m.group(2), t)
    vals = [int(x) for x in re.findall(r"(\d{3,7})\s*m\s*a\s*h", t)]
    vals = [v for v in vals if 500 <= v <= 500000]
    if vals:
        return max(vals)
    # "20K mAh" / "10k mah" shorthand
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*k\s*m\s*a\s*h", t):
        v = int(float(m.group(1)) * 1000)
        if 500 <= v <= 500000:
            vals.append(v)
    if vals:
        return max(vals)
    # Fallback: bare "Ah" (not preceded by 'm'), e.g. "40Ah" → 40000 mAh.
    # Negative lookbehind excludes the 'm' in 'mah'.
    for m in re.finditer(r"(?<!m)(?<!\d)(\d+(?:\.\d+)?)\s*ah\b", t):
        v = int(float(m.group(1)) * 1000)
        if 500 <= v <= 500000:
            vals.append(v)
    return max(vals) if vals else None


# Weight units, longest-first so the regex alternation prefers full words.
_WEIGHT_UNIT = r"(kilograms?|kg|grams?|gr|g|ounces?|oz|pounds?|lbs?|lb)"
# Amazon sprinkles invisible directionality / zero-width marks into spec tables.
_INVISIBLE = "‎‏​‪‫‬﻿"


def _to_grams(value, unit):
    u = unit.lower()
    if u.startswith("kg") or u.startswith("kilogram"):
        return value * 1000.0
    if u.startswith("lb") or u.startswith("pound"):
        return value * 453.592
    if u.startswith("oz") or u.startswith("ounce"):
        return value * 28.3495
    return value  # grams


# Plausible weight range in grams. Lower bound rejects misparses like "5G"→5 g.
# Upper bound accommodates large power stations (Jackery Explorer 2000 Pro ~19.5 kg,
# Anker SOLIX C1000 ~9 kg). Power banks well under 3 kg in practice.
WEIGHT_MIN_G = 40.0
WEIGHT_MAX_G = 30000.0


def extract_weight_g(text):
    """Weight in grams from Amazon weight strings, else None.

    Collects candidates in priority order — value next to a 'weight' label, then
    after a dimension separator ("...cm; 240 g"), then any weight token (heavier
    units first) — and returns the first one within a plausible range.
    """
    if not text:
        return None
    t = text
    for ch in _INVISIBLE:
        t = t.replace(ch, " ")
    t = t.lower().replace(",", "")

    candidates = []
    # Prefer specific product-weight labels over gross/shipping weights.
    # Priority: item/product/net weight > generic weight > gross/shipping weight.
    m = re.search(r"(?:item|product|net)\s+weight[^0-9]{0,15}(\d+(?:\.\d+)?)\s*" + _WEIGHT_UNIT + r"\b", t)
    if m:
        candidates.append(_to_grams(float(m.group(1)), m.group(2)))
    else:
        m = re.search(r"(?<!gross\s)(?<!shipping\s)weight[^0-9]{0,15}(\d+(?:\.\d+)?)\s*" + _WEIGHT_UNIT + r"\b", t)
        if m:
            candidates.append(_to_grams(float(m.group(1)), m.group(2)))
        else:
            # Last resort: gross or shipping weight (includes packaging)
            m = re.search(r"(?:gross|shipping)\s+weight[^0-9]{0,15}(\d+(?:\.\d+)?)\s*" + _WEIGHT_UNIT + r"\b", t)
            if m:
                candidates.append(_to_grams(float(m.group(1)), m.group(2)))
    m = re.search(r"[;:]\s*(\d+(?:\.\d+)?)\s*" + _WEIGHT_UNIT + r"\b", t)
    if m:
        candidates.append(_to_grams(float(m.group(1)), m.group(2)))
    for unit_re, factor in (
        (r"(\d+(?:\.\d+)?)\s*(?:kg|kilograms?)\b", 1000.0),
        (r"(\d+(?:\.\d+)?)\s*(?:lbs?|lb|pounds?)\b", 453.592),
        (r"(\d+(?:\.\d+)?)\s*(?:oz|ounces?)\b", 28.3495),
        (r"(\d+(?:\.\d+)?)\s*(?:grams?|gr|g)\b", 1.0),
    ):
        m = re.search(unit_re, t)
        if m:
            candidates.append(float(m.group(1)) * factor)

    for c in candidates:
        if WEIGHT_MIN_G <= c <= WEIGHT_MAX_G:
            return c
    return None


def extract_watts(text):
    """Return (pd_w, max_w). PD wattage if near 'PD'/'Power Delivery', plus the
    overall max output wattage. Wattage is also inferred from voltage/current
    pairs ("9V/2A" -> 18 W), ignoring AC mains input (V > 30)."""
    if not text:
        return (None, None)
    t = text.lower()
    nums = []
    # Explicit wattage tokens, decimals included ("22.5W", "65 W").
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*w(?:att)?s?\b", t):
        v = float(m.group(1))
        if 1 <= v <= 300:
            nums.append(v)
    # Voltage/current pairs -> watts ("5v/3a", "9v=2a", "9v 2a"); USB output only.
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*v\s*[/⎓·,=]?\s*(\d+(?:\.\d+)?)\s*a\b", t):
        volt, amp = float(m.group(1)), float(m.group(2))
        if 0 < volt <= 30 and 0 < amp <= 10:
            w = volt * amp
            if 1 <= w <= 300:
                nums.append(w)
    max_w = max(nums) if nums else None

    pd_w = None
    # Wattage before "PD" with optional port/tech label: "20W PD", "65W USB-C PD",
    # "100W GaN USB-C PD".
    for m in re.finditer(
            r"(\d+(?:\.\d+)?)\s*w\s*(?:(?:gan|usb[\s\-]?c|type[\s\-]?c)\s*)*(?:pd|power delivery)", t):
        v = float(m.group(1))
        if 1 <= v <= 300:
            pd_w = max(pd_w or 0, v)
    # Wattage after "PD": "PD 20W", "PD3.0 65W", "Power Delivery: 65W".
    for m in re.finditer(
            r"(?:pd|power delivery)(?:\d+(?:\.\d+)?)?\s*(?:charging|output|input)?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*w", t):
        v = float(m.group(1))
        if 1 <= v <= 300:
            pd_w = max(pd_w or 0, v)
    return (pd_w, max_w)


def extract_ports(text):
    """Return (usb_a, usb_c) counts (best-effort), else None for each.

    USB-C is matched explicitly. USB-A also counts a bare "USB" (the classic
    port), but not USB-C and not micro/mini-USB input. Word counts like "dual"
    are normalised to digits first.
    """
    if not text:
        return (None, None)
    t = text.lower()
    t = re.sub(r"\bdual\b", "2", t)
    t = re.sub(r"\btriple\b", "3", t)
    t = re.sub(r"\bquad(?:ruple)?\b", "4", t)
    usb_c = 0
    usb_a = 0

    for m in re.finditer(r"(\d+)\s*[x×]?\s*(?:usb[\s\-]?c|type[\s\-]?c)", t):
        usb_c = max(usb_c, int(m.group(1)))
    for m in re.finditer(r"(?:usb[\s\-]?c|type[\s\-]?c)\s*[x×]\s*(\d+)", t):
        usb_c = max(usb_c, int(m.group(1)))

    for m in re.finditer(r"(\d+)\s*[x×]?\s*(?:usb[\s\-]?a|type[\s\-]?a)", t):
        usb_a = max(usb_a, int(m.group(1)))
    for m in re.finditer(r"(?:usb[\s\-]?a|type[\s\-]?a)\s*[x×]\s*(\d+)", t):
        usb_a = max(usb_a, int(m.group(1)))
    # bare "USB" = USB-A, excluding USB-C and micro/mini-USB (usually input)
    tb = re.sub(r"(?:micro|mini)[\s\-]?usb", " ", t)
    for m in re.finditer(r"(\d+)\s*[x×]?\s*usb(?![\s\-]?c)\b", tb):
        usb_a = max(usb_a, int(m.group(1)))

    if usb_c == 0 and re.search(r"usb[\s\-]?c|type[\s\-]?c", t):
        usb_c = 1
    if usb_a == 0 and re.search(r"usb(?![\s\-]?c)\b", tb):
        usb_a = 1
    return (usb_a or None, usb_c or None)


def _has_any(text, keys):
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in keys)


EXPANDABLE_KEYS = ["expandable", "expansion battery", "extra battery", "modular capacity",
                   "expand capacity", "add-on battery"]


def detect_features(text):
    t = (text or "").lower()
    return {
        "wireless": _has_any(text, WIRELESS_KEYS),
        "display": _has_any(text, DISPLAY_KEYS),
        "passthrough": _has_any(text, PASSTHROUGH_KEYS),
        "solar": _has_any(text, SOLAR_KEYS),
        "expandable": any(k in t for k in EXPANDABLE_KEYS),
        "ups": "uninterruptible" in t or bool(re.search(r"\bups\b", t)),
    }


# ---- power-station parsers ----
POWER_STATION_KEYS = ["power station", "portable power station", "solar generator",
                      "portable generator", "lifepo4 generator", "leisure battery",
                      "energy storage station", "energy station", "battery generator"]


def is_power_station(title):
    """True if the listing is a portable power station (not an accessory)."""
    t = (title or "").lower()
    if not t or any(k in t for k in STRONG_EXCLUDE):
        return False
    if any(k in t for k in POWER_STATION_KEYS):
        return True
    # High-Wh capacity in the title (≥200 Wh) implies a power station even
    # without a keyword — power banks top out well below this range.
    wh = extract_wh(title)
    return wh is not None and wh >= 200


def extract_chemistry(text):
    """Battery chemistry: lifepo4 | li-po | nmc | lto | li-ion, else None."""
    if not text:
        return None
    t = text.lower()
    if any(k in t for k in ("lifepo4", "lifepo", "lfp", "lithium iron phosphate",
                            "lithium ferro phosphate", "lithium ferrophosphate",
                            "lithium-iron-phosphate", "iron phosphate", "li-fe")):
        return "lifepo4"
    if "lithium titanate" in t or re.search(r"\blto\b", t):
        return "lto"
    if any(k in t for k in ("nmc", "ncm", "nca", "ternary lithium",
                            "nickel cobalt", "nickel manganese")):
        return "nmc"
    if any(k in t for k in ("lithium polymer", "li-polymer", "li-po", "li po",
                            "li polymer", "lipo", "polymer battery", "polymer cell")):
        return "li-po"
    if any(k in t for k in ("lithium-ion", "lithium ion", "li-ion", "liion",
                            "lithium battery", "lithium batteries")):
        return "li-ion"
    return None


_WH_SKIP_PRECEDERS = (
    "expandable", "expanded", "expand to", "expand it to", "expand up to",
    "up to", "maximum", "max ", "increase to", "extend to", "with extra",
    "with additional", "scalable to", "scales to",
)


def extract_wh(text):
    """Energy capacity in Wh (handles kWh), else None.

    Filters out values describing the *expanded* maximum (e.g. '2016Wh
    expandable to 20kWh' should be 2016, not 20000) by skipping numbers
    preceded by phrases like 'expandable', 'up to', 'extend to'. Returns the
    largest of the remaining plausible values."""
    if not text:
        return None
    t = text.lower().replace(",", "")

    def _skipped(start):
        window = t[max(0, start - 32):start]
        return any(k in window for k in _WH_SKIP_PRECEDERS)

    vals = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*kwh", t):
        if not _skipped(m.start()):
            vals.append(float(m.group(1)) * 1000)
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*whr?\b", t):
        if not _skipped(m.start()):
            vals.append(float(m.group(1)))
    vals = [v for v in vals if 50 <= v <= 20000]
    return max(vals) if vals else None


def extract_ac_output_w(text):
    """Continuous AC inverter wattage (ignores surge/peak), else None."""
    if not text:
        return None
    t = text.lower()
    surge = set()  # wattages explicitly labelled surge/peak — excluded
    for m in re.finditer(r"(\d{3,4})\s*w\s*(?:surge|peak)", t):
        surge.add(int(m.group(1)))
    # surge directly followed by the number (no comma) — "surge 2000W", "peak: 2000W"
    for m in re.finditer(r"(?:surge|peak)\s*:?\s*(\d{3,4})\s*w", t):
        surge.add(int(m.group(1)))
    best = None
    for m in re.finditer(r"(\d{3,4})\s*w\b", t):
        v = int(m.group(1))
        if not (100 <= v <= 8000) or v in surge:
            continue
        window = t[max(0, m.start() - 25):m.end() + 12]
        if any(k in window for k in ("ac", "output", "inverter", "sine", "rated",
                                     "continuous", "mains", "socket", "plug")):
            best = max(best or 0, v)
    # Fallback for power-station titles/specs that lead with the rated wattage
    # without an explicit "AC output" label — e.g. "Power Station, 1800W (Peak 2400W)".
    _station_terms = ("power station", "power bank station", "energy station",
                      "energy storage station", "battery generator")
    if best is None and any(k in t for k in _station_terms):
        for m in re.finditer(r"(\d{3,4})\s*w\b", t):
            v = int(m.group(1))
            if 100 <= v <= 8000 and v not in surge:
                best = max(best or 0, v)
    return best


def extract_ac_sockets(text):
    """Number of AC outlets/sockets, else None."""
    if not text:
        return None
    t = text.lower()
    cnt = 0
    _socket_kws = (r"ac (?:output|socket|outlet|port)", r"uk (?:socket|plug|outlet)",
                   r"mains (?:socket|outlet|plug)", r"schuko", r"household (?:socket|outlet)",
                   r"standard (?:socket|outlet)", r"grounded (?:socket|outlet)")
    _kw_pat = "|".join(_socket_kws)
    for m in re.finditer(rf"(\d+)\s*[x×]?\s*(?:{_kw_pat})", t):
        cnt = max(cnt, int(m.group(1)))
    for m in re.finditer(rf"(?:{_kw_pat})s?\s*[x×]\s*(\d+)", t):
        cnt = max(cnt, int(m.group(1)))
    if cnt == 0 and re.search(_kw_pat, t):
        cnt = 1
    return cnt or None


def extract_solar_input_w(text):
    """Max solar input wattage if solar/MPPT is mentioned, else None."""
    if not text:
        return None
    t = text.lower()
    _solar_terms = ("solar", "mppt", "photovoltaic", "pv input")
    if not any(k in t for k in _solar_terms):
        return None
    best = None
    for m in re.finditer(r"(\d{2,4})\s*w\b", t):
        v = int(m.group(1))
        window = t[max(0, m.start() - 25):m.end() + 15]
        if any(k in window for k in _solar_terms) and 10 <= v <= 2000:
            best = max(best or 0, v)
    return best


def extract_cycle_life(text):
    """Rated charge cycles (e.g. '3000 cycles', '2000 recharges'), else None."""
    if not text:
        return None
    t = text.lower().replace(",", "")
    patterns = [
        r"(\d{3,5})\s*\+?\s*(?:charge[\s\-](?:discharge[\s\-])?)?cycles?",
        r"(\d{3,5})\s*\+?\s*(?:full\s*)?recharges?",
        r"(?:life\s*cycles?|cycle\s*life)[^\d]{0,20}(\d{3,5})",
        r"(?:recharg\w*|charg\w*)\s+(\d{3,5})\s*\+?\s*times?",
        r"(\d{3,5})\s*\+?\s*times?\s+(?:recharg|charg)",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            v = int(m.group(m.lastindex))
            if 100 <= v <= 20000:
                return v
    return None


def parse_price(text):
    if not text:
        return None
    m = re.search(r"£\s*([\d,]+(?:\.\d{1,2})?)", text)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"([\d,]+\.\d{2})", text)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


def extract_date_first_available(text):
    """ISO date (YYYY-MM-DD) from the 'Date First Available' / 'Date First Listed'
    row of an Amazon product-details table, else None."""
    if not text:
        return None
    t = text
    for ch in _INVISIBLE:
        t = t.replace(ch, " ")
    low = t.lower()
    idx = low.find("date first available")
    if idx < 0:
        idx = low.find("date first listed")
    if idx < 0:
        return None
    window = t[idx:idx + 60]
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})", window)   # 12 May 2023
    if m and m.group(2).lower() in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", window)  # May 12, 2023
    if m and m.group(1).lower() in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", window)  # 2023-05-12 ISO
    if m:
        return m.group(0)
    return None


_VOWELS = set("AEIOUaeiou")


def _looks_like_seller_code(s):
    """True for the random-looking all-caps strings Amazon falls back to when a
    product has no real brand (e.g. 'HVSYVVSRL', 'HIJYMNZPQ'). The rule:
    >=6 alphabetic chars, fully uppercase, and at most one vowel — real brand
    names have multiple vowels even when stylised as all-caps (INIU, UGREEN,
    AUKEY, ANKER all pass)."""
    if not s or len(s) < 6 or not s.isalpha() or not s.isupper():
        return False
    vowels = sum(1 for c in s if c in _VOWELS)
    return vowels <= 1


def brand_from_title(title, known_brands=()):
    """Recover the brand from the title when the byline gave only a seller code.

    Prefers an exact match against the supplied `known_brands` list (so a title
    starting with 'EF ECOFLOW DELTA Pro 3' returns 'EF ECOFLOW', and 'VTOMAN
    J1500 Power Station' returns 'VTOMAN'). Falls back to the first all-caps
    token (length >= 3) when no known brand matches, provided it doesn't itself
    look like a seller code."""
    if not title:
        return None
    t = title.strip()
    if not t:
        return None
    lower = t.lower()
    # Longest known-brand match wins — 'EF ECOFLOW' beats 'ECOFLOW'.
    candidates = sorted({b for b in known_brands if b}, key=len, reverse=True)
    for b in candidates:
        if lower.startswith(b.lower() + " ") or lower == b.lower():
            return b
    m = re.match(r"\s*([A-Z][A-Z0-9-]{2,})\b", t)
    if m:
        cand = m.group(1)
        if not _looks_like_seller_code(cand):
            return cand
    return None


def clean_brand(text):
    """Normalise an Amazon byline into a bare brand name.

    Handles "Visit the X Store", "Brand: X", "by X", and a trailing "Store".
    Returns None for byline values that look like Amazon seller codes rather
    than real brand names — these otherwise pollute the brand reputation model.
    """
    if not text:
        return None
    b = text.strip()
    _STORE_QUALIFIERS = r"(?:official|uk\b|direct|online|exclusive|authoris[eo]d|shop)"
    m = re.search(r"visit the\s+(.+?)\s+store\b", b, re.I)
    if m:
        b = m.group(1)
    b = re.sub(r"^\s*(?:brand|name|manufacturer)[:\s]+", "", b, flags=re.I)
    b = re.sub(r"^\s*by\s+", "", b, flags=re.I)
    b = re.sub(r"\s+store$", "", b, flags=re.I)
    # Strip trailing Amazon store qualifiers that aren't part of the brand name.
    b = re.sub(r"\s+" + _STORE_QUALIFIERS + r"\s*$", "", b, flags=re.I)
    # Strip corporate suffixes: "Jackery, Inc." -> "Jackery"
    b = re.sub(r",?\s*\b(?:inc|ltd|llc|co|corp|plc|gmbh|bv|ag|s\.a|s\.l|oy|ab)\b\.?\s*$", "", b, flags=re.I)
    # Strip trailing parenthetical qualifiers: "Anker (Official)" -> "Anker"
    b = re.sub(r"\s*\([^)]*\)\s*$", "", b)
    b = b.strip(" :,|-‎‏")
    if _looks_like_seller_code(b):
        return None
    return b or None


def parse_rating(text):
    if not text:
        return None
    m = re.search(r"([0-5](?:\.\d)?)\s*out of\s*5", text.lower())
    return float(m.group(1)) if m else None


def parse_review_count(text):
    """Review/rating count from varied formats: '1,234 ratings', bare '1,234',
    '(1,234)', '1.2K'. Returns None for rating text like '4.5 out of 5 stars'."""
    if not text:
        return None
    t = text.lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*([km])?\s*\+?\s*(?:global\s+)?(?:ratings?|reviews?)", t)
    if not m:
        m = re.search(r"^\(?\s*(\d+(?:\.\d+)?)\s*([km])?\s*\+?\s*\)?\s*$", t.strip())
    if not m:
        return None
    val = float(m.group(1))
    suffix = m.group(2)
    if suffix == "k":
        val *= 1_000
    elif suffix == "m":
        val *= 1_000_000
    return int(val)
