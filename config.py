"""Configuration for the battery bank aggregator (env-overridable)."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Server
HOST = os.environ.get("BBA_HOST", "127.0.0.1")
PORT = _int("BBA_PORT", 8473)  # non-dev port

# Scraping
SEARCH_URL = os.environ.get(
    "BBA_SEARCH_URL",
    "https://www.amazon.co.uk/s?k=battery+bank&s=review-rank",
)
# Searches to scrape, each tagged with a product category. Add more {category,url}
# entries here; categories drive relevance, metrics, features, filters and pages.
SEARCHES = [
    {"category": "power_bank", "url": SEARCH_URL},
    {"category": "power_station",
     "url": "https://www.amazon.co.uk/s?k=lifepo4+power+station&s=review-rank"},
]
CATEGORIES = ["power_bank", "power_station"]
TOP_N = _int("BBA_TOP_N", 0)  # 0 = unlimited: scrape until blocked or no more results
MAX_PAGES = _int("BBA_MAX_PAGES", 30)  # hard safety bound on pagination
# Two-cadence scrape:
#   - Hourly: refresh the N oldest products by last_seen via direct detail pages
#     (no search pagination). Keeps prices fresh on a rolling window, small per-
#     run Amazon-load so a single CAPTCHA doesn't trash much work.
#   - Discovery (once a day): full search pagination across the configured queries
#     to find new ASINs. Heavy but rare.
INTERVAL_HOURS = _int("BBA_INTERVAL_HOURS", 1)
HOURLY_BATCH_SIZE = _int("BBA_HOURLY_BATCH_SIZE", 40)
DISCOVERY_INTERVAL_HOURS = _int("BBA_DISCOVERY_INTERVAL_HOURS", 24)
# After Amazon serves a CAPTCHA, back off for this many hours before any new
# scrape attempt — each failed-attempt-during-rep-trouble worsens the IP's
# standing, and the only thing that actually clears it is silence.
CAPTCHA_BACKOFF_HOURS = _int("BBA_CAPTCHA_BACKOFF_HOURS", 6)
# A product is treated as delisted (and removed) only after it has been absent
# from results for this long — avoids churn-deleting items that merely drop out
# of one run's shifting search results.
REMOVE_AFTER_HOURS = _int("BBA_REMOVE_AFTER_HOURS", 30)
HEADLESS = os.environ.get("BBA_HEADLESS", "1") != "0"
MIN_DELAY_S = float(os.environ.get("BBA_MIN_DELAY_S", "2"))
MAX_DELAY_S = float(os.environ.get("BBA_MAX_DELAY_S", "6"))
LOCALE = "en-GB"
USER_AGENT = os.environ.get(
    "BBA_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

# Storage. Paths are env-overridable so the container can point them at /data
# (a single mounted volume) and dev keeps the in-tree defaults.
DB_PATH = os.environ.get("BBA_DB", os.path.join(BASE_DIR, "battery_banks.sqlite3"))
LOG_DIR = os.environ.get("BBA_LOG_DIR", os.path.join(BASE_DIR, "logs"))
CACHE_DIR = os.environ.get("BBA_CACHE_DIR", os.path.join(BASE_DIR, "cache"))
STATE_DIR = os.environ.get("BBA_STATE_DIR", os.path.join(BASE_DIR, "state"))
# Detail pages are practically static per ASIN (specs don't change); a long TTL
# lets parser re-runs replay against cached HTML without re-fetching.
CACHE_TTL_HOURS = _int("BBA_CACHE_TTL_HOURS", 24 * 14)

# Brand-anchored searches. Each cycle picks one brand per category (rotation
# driven by the date) and adds its search URL to the configured SEARCHES. Over
# enough days every brand gets covered without raising per-run request volume.
BRAND_BANKS = [
    "Anker", "UGREEN", "Baseus", "INIU", "Charmast",
    "VEGER", "Romoss", "Cuktech", "Zendure", "Xiaomi",
]
BRAND_STATIONS = [
    "EcoFlow", "Jackery", "BLUETTI", "Anker SOLIX", "Goal Zero",
    "Allpowers", "Oukitel", "Fossibot", "VTOMAN", "Pecron",
]

# Physics: Li-ion energy density ceiling (generous) and nominal cell voltage.
LIION_MAX_WH_PER_KG = 260.0
NOMINAL_CELL_VOLTAGE = 3.7

# Energy-density ceiling (Wh/kg) by chemistry — drives the chemistry-aware physics
# check. LiFePO4 is much less dense than Li-ion, so its packs are heavier per Wh.
ENERGY_DENSITY_WH_PER_KG = {
    "lifepo4": 160.0, "li-ion": 260.0, "li-po": 255.0, "nmc": 250.0,
    "default": 260.0,
}

# Honesty scoring default weights (UI-adjustable). Physics weighted highest.
DEFAULT_HONESTY_WEIGHTS = {
    "physics": 0.40,
    "price": 0.25,
    "brand": 0.20,
    "reviews": 0.15,
}

# Brand reputation (for unverified brands): a volume-weighted average rating,
# shrunk toward the catalog mean, then penalised for fakes.
BRAND_PRIOR_REVIEWS = _int("BBA_BRAND_PRIOR_REVIEWS", 50)  # shrinkage strength (review units)
BRAND_RATING_FLOOR = float(os.environ.get("BBA_BRAND_RATING_FLOOR", "2.5"))  # rating mapped to 0
BRAND_FAKE_PENALTY = float(os.environ.get("BBA_BRAND_FAKE_PENALTY", "0.5"))  # max rep cut if all flagged

# Curated good-brand list (lowercase match, substring allowed).
TRUSTED_BRANDS = [
    "Anker", "Baseus", "INIU", "UGREEN", "RAVPower", "Belkin", "Zendure",
    "AUKEY", "Mophie", "Xiaomi", "Samsung", "Spigen", "OtterBox", "Goal Zero",
    "EcoFlow", "Nitecore", "Charmast", "VEGER", "Romoss", "Cuktech",
    "Sharge", "Shargeek", "BioLite", "Jackery", "Poweradd", "Omnicharge",
]

# Review phrases that suggest overstated / fake capacity.
FAKE_PHRASES = [
    "won't hold charge", "wont hold charge", "doesn't hold charge",
    "does not hold charge", "not as advertised", "fake mah", "fake capacity",
    "drains fast", "drains quickly", "dies quickly", "nowhere near",
    "not the capacity", "false capacity", "overstated", "barely charges",
    "only charges once", "less than advertised", "way less", "much less",
    "feels too light", "feels light for", "won't even charge",
    "doesn't even charge", "lies about", "scam", "misleading capacity",
    "advertised mah", "no way it's", "no way its",
]

# LiFePO4 cells sit at ~3.2 V nominal vs Li-ion ~3.7 V; this matters for the
# mAh<->Wh self-consistency check (a 100Wh LiFePO4 pack is ~31Ah, not ~27Ah).
NOMINAL_VOLTAGE_BY_CHEMISTRY = {
    "lifepo4": 3.2, "li-ion": 3.7, "li-po": 3.7, "nmc": 3.7, "lto": 2.3,
}
