"""Derived metrics: cost-per-mAh, honesty sub-scores, hedonic regression, feature value."""
import logging
import warnings

import numpy as np

import config
import parse
from models import Product, Meta

log = logging.getLogger("analysis")

# Resolve the NNLS solver once at import. If scipy is genuinely missing we want to
# know (the model degrades to a clipped OLS), not silently mask it per-call.
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # scipy's NumPy-version notice is noise
        from scipy.optimize import nnls as _nnls
except Exception:  # pragma: no cover - scipy expected to be present
    _nnls = None
    log.warning("scipy.optimize.nnls unavailable; hedonic model will use clipped OLS")


def _clean_weight(weight_g):
    """Ignore stored weights outside the plausible range (guards against stale
    misparses like a 5 g pack from older scrapes)."""
    if weight_g and parse.WEIGHT_MIN_G <= weight_g <= parse.WEIGHT_MAX_G:
        return weight_g
    return None

# Regression feature order, per category.
FEATURE_NAMES_BY_CAT = {
    "power_bank": ["capacity_kmah", "pd_w", "usb_c", "wireless", "display",
                   "passthrough", "solar", "brand_trust", "rating"],
    "power_station": ["capacity_kwh", "ac_output_w", "ac_sockets", "solar_input_w",
                      "pd_w", "cycle_life", "expandable", "ups", "brand_trust", "rating"],
}


def feature_names(category):
    return FEATURE_NAMES_BY_CAT.get(category, FEATURE_NAMES_BY_CAT["power_bank"])


def energy_density(chemistry):
    d = config.ENERGY_DENSITY_WH_PER_KG
    return d.get((chemistry or "").lower(), d["default"])


def nominal_voltage(chemistry):
    """Per-cell nominal voltage for the mAh<->Wh conversion. Defaults to Li-ion."""
    return config.NOMINAL_VOLTAGE_BY_CHEMISTRY.get(
        (chemistry or "").lower(), config.NOMINAL_CELL_VOLTAGE)


def self_consistency_subscore(claimed_mah, capacity_wh, chemistry=None):
    """When a listing claims BOTH mAh and Wh, the two should agree at the cell
    voltage. Large mismatches indicate one side was inflated (almost always the
    bigger number). Returns 1.0 inside a ±25% window, falling to 0 as the
    mismatch grows; flag set once outside that window."""
    if not claimed_mah or not capacity_wh:
        return None, None
    expected_wh = claimed_mah / 1000.0 * nominal_voltage(chemistry)
    if expected_wh <= 0:
        return None, None
    ratio = capacity_wh / expected_wh
    if 0.75 <= ratio <= 1.25:
        return 1.0, None
    # ratio>1: Wh inflated relative to mAh. ratio<1: mAh inflated.
    score = max(0.0, 1.0 - abs(ratio - 1.0))
    return score, "inconsistent_capacity_claims"


def _edit_distance(a, b):
    """Plain Levenshtein. Short-circuits when the length gap already exceeds 2
    so iterating over the brand catalog is cheap."""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    if len(a) < len(b):
        a, b = b, a
    if len(a) - len(b) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def brand_mimicry_flag(brand):
    """Flag non-trusted brands one edit away from a trusted brand (e.g. 'Ankur'
    for 'Anker'). Skipped when brand_trusted() already accepts the name — that
    handles legitimate variants like 'Anker Innovations'. Trusted brand names
    shorter than 4 chars are excluded to avoid spurious matches."""
    if not brand:
        return None
    b = brand.strip().lower()
    if not b or brand_trusted(brand):
        return None
    for tb in config.TRUSTED_BRANDS:
        tbl = tb.lower()
        if len(tbl) < 4:
            continue
        if _edit_distance(b, tbl) <= 1:
            return f"brand_mimics_{tbl}"
    return None


def capacity_wh_of(p):
    """Energy capacity in Wh: stored (power stations) or derived from mAh (power banks)."""
    if p.capacity_wh:
        return p.capacity_wh
    if p.claimed_mah:
        return p.claimed_mah / 1000.0 * config.NOMINAL_CELL_VOLTAGE
    return None


def brand_trusted(brand):
    if not brand:
        return False
    b = brand.strip().lower()
    return any(tb.lower() == b or tb.lower() in b for tb in config.TRUSTED_BRANDS)


def cost_per_mah(price, mah):
    if price and mah and mah > 0:
        return price / mah
    return None


def cost_per_wh(price, wh):
    if price and wh and wh > 0:
        return price / wh
    return None


def physics_subscore(capacity_wh, weight_g, chemistry=None):
    """1.0 = plausible weight for the energy capacity; <1 penalises an impossibly
    light pack. Uses the chemistry's energy-density ceiling (LiFePO4 < Li-ion)."""
    weight_g = _clean_weight(weight_g)
    if not capacity_wh or not weight_g or weight_g <= 0:
        return None, None
    min_weight_g = capacity_wh / energy_density(chemistry) * 1000.0
    if min_weight_g <= 0:
        return None, None
    ratio = weight_g / min_weight_g
    score = max(0.0, min(1.0, ratio))
    flag = "impossible_capacity" if ratio < 0.95 else None
    return score, flag


def max_plausible_wh(weight_g, chemistry=None):
    """Upper bound on energy (Wh) a pack of this weight could hold for its chemistry."""
    weight_g = _clean_weight(weight_g)
    if not weight_g or weight_g <= 0:
        return None
    return weight_g / 1000.0 * energy_density(chemistry)


def max_plausible_mah(weight_g, chemistry=None):
    """Plausible Wh ceiling expressed as mAh at the nominal cell voltage."""
    wh = max_plausible_wh(weight_g, chemistry)
    return wh / config.NOMINAL_CELL_VOLTAGE * 1000.0 if wh else None


def brand_subscore(brand):
    if not brand:
        return 0.5, "unknown_brand"
    if brand_trusted(brand):
        return 1.0, None
    return 0.5, "unverified_brand"


# Flags that count against a brand's reputation (clear fakery, not soft signals).
_FAKE_FLAGS = ("impossible_capacity", "reviews_report_fake_capacity")


def brand_reputations(products):
    """Map brand (lowercased) -> data-grounded reputation in [0,1], or None when
    the brand has no rated products. Volume-weighted average rating, shrunk toward
    the catalog mean for low-data brands, then cut by the fraction of the brand's
    products flagged as fake."""
    agg = {}  # brand -> {r_rv, rv, n, flagged, rated}
    tot_r_rv = tot_rv = 0.0
    for p in products:
        b = (p.brand or "").strip().lower()
        if not b:
            continue
        d = agg.setdefault(b, {"r_rv": 0.0, "rv": 0.0, "n": 0, "flagged": 0, "rated": 0})
        d["n"] += 1
        if any(f in (p.honesty_flags or []) for f in _FAKE_FLAGS):
            d["flagged"] += 1
        if p.rating is not None:
            w = p.review_count if (p.review_count and p.review_count > 0) else 1
            d["r_rv"] += p.rating * w
            d["rv"] += w
            d["rated"] += 1
            tot_r_rv += p.rating * w
            tot_rv += w
    if tot_rv <= 0:
        return {}
    catalog_mean = tot_r_rv / tot_rv
    m = config.BRAND_PRIOR_REVIEWS
    floor = config.BRAND_RATING_FLOOR
    reps = {}
    for b, d in agg.items():
        if d["rated"] == 0:
            reps[b] = None
            continue
        weighted = d["r_rv"] / d["rv"]
        v = d["rv"]
        adj = (v * weighted + m * catalog_mean) / (v + m)   # Bayesian shrinkage
        rep_rating = max(0.0, min(1.0, (adj - floor) / (5.0 - floor)))
        flagged_frac = d["flagged"] / d["n"] if d["n"] else 0.0
        rep = rep_rating * (1.0 - config.BRAND_FAKE_PENALTY * flagged_frac)
        reps[b] = round(max(0.0, min(1.0, rep)), 3)
    return reps


def brand_honesty(brand, reps):
    """Reputation-aware brand sub-score: trusted -> 1.0; unverified -> data-grounded
    reputation (falling back to 0.5 when the brand has no rating data).

    The 'unverified_brand' flag is only set when reputation is missing or low
    (<0.55) — a non-trusted brand with strong volume-and-rating data shouldn't
    carry a caution flag, since the data already shows it can be trusted."""
    if not brand:
        return 0.5, "unknown_brand"
    if brand_trusted(brand):
        return 1.0, None
    rep = reps.get(brand.strip().lower())
    if rep is None:
        return 0.5, "unverified_brand"
    flag = "unverified_brand" if rep < 0.55 else None
    return rep, flag


def reviews_subscore(snippets):
    if not snippets:
        return None, None
    text = " ".join(snippets).lower()
    hits = sum(text.count(p) for p in config.FAKE_PHRASES)
    score = max(0.0, 1.0 - min(1.0, hits / 3.0))
    flag = "reviews_report_fake_capacity" if hits >= 2 else None
    return score, flag


def price_outlier_subscores(products, metric_attr="cost_per_mah"):
    """asin -> (score, flag) from a robust (median/MAD) z-score of the cost metric
    (cost_per_mah for banks, cost_per_wh for stations) within the given group."""
    pairs = [(p, getattr(p, metric_attr)) for p in products if getattr(p, metric_attr)]
    res = {}
    if len(pairs) < 5:
        return {p.asin: (1.0, None) for p, _ in pairs}
    arr = np.array([v for _, v in pairs])
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med))) or 1e-9
    for p, v in pairs:
        z = (v - med) / (1.4826 * mad)
        if z < -2.5:
            res[p.asin] = (0.2, "too_cheap_per_capacity")
        elif z < -1.5:
            res[p.asin] = (0.6, None)
        else:
            res[p.asin] = (1.0, None)
    return res


def _feature_vector(p, category):
    if category == "power_station":
        return [
            (p.capacity_wh or 0) / 1000.0,   # capacity_kwh
            p.ac_output_w or 0,
            p.ac_sockets or 0,
            p.solar_input_w or 0,
            p.pd_w or 0,
            p.cycle_life or 0,
            1.0 if p.expandable else 0.0,
            1.0 if p.ups else 0.0,
            1.0 if brand_trusted(p.brand) else 0.0,
            p.rating or 0,
        ]
    return [
        (p.claimed_mah or 0) / 1000.0,
        p.pd_w or 0,
        p.usb_c or 0,
        1.0 if p.wireless else 0.0,
        1.0 if p.display else 0.0,
        1.0 if p.passthrough else 0.0,
        1.0 if p.solar else 0.0,
        1.0 if brand_trusted(p.brand) else 0.0,
        p.rating or 0,
    ]


def _has_capacity(p, category):
    return bool(p.capacity_wh) if category == "power_station" else bool(p.claimed_mah)


def hedonic_model(products, category):
    """Non-negative hedonic price model for one category. Returns
    {intercept, coef:{name:beta}, fallback} or None if too few rows."""
    names = feature_names(category)
    rows, ys = [], []
    for p in products:
        if p.price and _has_capacity(p, category):
            rows.append(_feature_vector(p, category))
            ys.append(p.price)
    if len(rows) < len(names) + 2:
        return None
    A = np.column_stack([np.ones(len(rows)), np.array(rows)])
    y = np.array(ys)
    fallback = _nnls is None
    if _nnls is not None:
        try:
            coef, _ = _nnls(A, y)
        except Exception as e:  # genuine numerical failure, not a missing import
            log.warning("NNLS failed (%s); falling back to clipped OLS", e)
            fallback = True
    if fallback:
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        coef = np.clip(coef, 0.0, None)
    return {
        "intercept": float(coef[0]),
        "coef": {n: float(b) for n, b in zip(names, coef[1:])},
        "fallback": fallback,
    }


def fair_price(model, p, category):
    if not model:
        return None
    c = model["coef"]
    v = model["intercept"]
    for name, val in zip(feature_names(category), _feature_vector(p, category)):
        v += c[name] * val
    return float(v)


def feature_contributions(model, p, category):
    """£ value each selectable feature adds (clamped >= 0 for value-to-you)."""
    if not model:
        return {}
    c = model["coef"]
    if category == "power_station":
        raw = {
            "capacity": c["capacity_kwh"] * ((p.capacity_wh or 0) / 1000.0),
            "ac_output_w": c["ac_output_w"] * (p.ac_output_w or 0),
            "ac_sockets": c["ac_sockets"] * (p.ac_sockets or 0),
            "solar_input_w": c["solar_input_w"] * (p.solar_input_w or 0),
            "pd_w": c["pd_w"] * (p.pd_w or 0),
            "cycle_life": c["cycle_life"] * (p.cycle_life or 0),
            "expandable": c["expandable"] if p.expandable else 0.0,
            "ups": c["ups"] if p.ups else 0.0,
            "rating": c["rating"] * (p.rating or 0),
        }
    else:
        raw = {
            "capacity": c["capacity_kmah"] * ((p.claimed_mah or 0) / 1000.0),
            "pd_w": c["pd_w"] * (p.pd_w or 0),
            "usb_c": c["usb_c"] * (p.usb_c or 0),
            "wireless": c["wireless"] if p.wireless else 0.0,
            "display": c["display"] if p.display else 0.0,
            "passthrough": c["passthrough"] if p.passthrough else 0.0,
            "solar": c["solar"] if p.solar else 0.0,
            "rating": c["rating"] * (p.rating or 0),
        }
    return {k: round(max(0.0, float(v)), 2) for k, v in raw.items()}


def _set_meta(session, key, value):
    meta = session.get(Meta, key)
    if meta is None:
        meta = Meta(key=key)
        session.add(meta)
    meta.value = value


def run_analysis(session):
    """Recompute all derived fields, computing market signals (outliers, regression,
    brand reputation) within each category. Returns {category: model}."""
    products = session.query(Product).all()

    # Derive capacity (Wh) and cost metrics for everything.
    for p in products:
        if p.category != "power_station" and not p.capacity_wh and p.claimed_mah:
            p.capacity_wh = round(p.claimed_mah / 1000.0 * config.NOMINAL_CELL_VOLTAGE, 1)
        p.cost_per_mah = cost_per_mah(p.price, p.claimed_mah)
        p.cost_per_wh = cost_per_wh(p.price, p.capacity_wh)

    by_cat = {}
    for p in products:
        by_cat.setdefault(p.category or "power_bank", []).append(p)

    models = {}
    reps_all = {}
    for category, group in by_cat.items():
        metric = "cost_per_wh" if category == "power_station" else "cost_per_mah"
        outliers = price_outlier_subscores(group, metric)
        model = hedonic_model(group, category)
        models[category] = model or {}

        # Pass 1: per-product signals (chemistry-aware physics + price + reviews
        # + mAh<->Wh self-consistency).
        for p in group:
            cap_wh = capacity_wh_of(p)
            physics, f_phys = physics_subscore(cap_wh, p.weight_g, p.chemistry)
            reviews, f_rev = reviews_subscore(p.review_snippets)
            price_s, f_price = outliers.get(p.asin, (None, None))
            cons, f_cons = self_consistency_subscore(p.claimed_mah, p.capacity_wh, p.chemistry)
            p.honesty = {"physics": physics, "price": price_s, "brand": None,
                         "reviews": reviews, "consistency": cons}
            plausible_wh = max_plausible_wh(p.weight_g, p.chemistry)
            if plausible_wh and cap_wh:
                if category == "power_station":
                    p.honesty["wh_cap"] = round(plausible_wh)
                else:
                    p.honesty["mah_cap"] = round(plausible_wh / config.NOMINAL_CELL_VOLTAGE * 1000)
                if cap_wh > plausible_wh:
                    p.honesty["overstatement_pct"] = round(100 * (cap_wh - plausible_wh) / plausible_wh)
            p.honesty_flags = [f for f in (f_phys, f_price, f_rev, f_cons) if f]  # brand flag added below

        reps = brand_reputations(group)
        reps_all.update(reps)

        # Pass 2: reputation-aware brand sub-score, fair price, feature value.
        for p in group:
            brand, f_brand = brand_honesty(p.brand, reps)
            p.honesty = dict(p.honesty, brand=brand)  # new dict -> JSON column flagged dirty
            extra_flags = [f for f in (f_brand, brand_mimicry_flag(p.brand)) if f]
            if extra_flags:
                p.honesty_flags = (p.honesty_flags or []) + extra_flags
            fp = fair_price(model, p, category)
            p.fair_price = round(fp, 2) if fp is not None else None
            p.price_delta = round(p.price - fp, 2) if (fp is not None and p.price) else None
            p.feature_contrib = feature_contributions(model, p, category)

    _set_meta(session, "feature_model", models)
    _set_meta(session, "brand_reputation", reps_all)
    session.commit()
    return models
