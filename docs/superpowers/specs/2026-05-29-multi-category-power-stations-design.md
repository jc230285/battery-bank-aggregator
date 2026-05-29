# Multi-search, Power-Station category & battery-tech filter — Design

**Date:** 2026-05-29
**Status:** Approved design, pending implementation

## Purpose

Expand the aggregator from a single power-bank catalog to **multiple Amazon searches across
two product categories** — `power_bank` (mAh-rated) and `power_station` (Wh-rated, mostly
LiFePO4) — served as **two pages** with category-appropriate metrics, features, filters, and
value analysis, plus a **battery-chemistry filter**.

Seed searches:
- power_bank: `https://www.amazon.co.uk/s?k=battery+bank&s=review-rank`
- power_station: `https://www.amazon.co.uk/s?k=lifepo4+power+station`

## Goals
- Scrape several searches, each tagged with a category; dedupe by ASIN.
- Two UI pages (top toggle): Power Banks (cost-per-mAh) and Power Stations (cost-per-Wh).
- Classify battery chemistry (LiFePO4 / Li-ion / Li-polymer / NMC / unknown) → filter + drives
  chemistry-aware physics.
- Category-specific **features** (value-to-you factors) and **filters**.
- Per-category analysis (price outliers, hedonic regression, brand reputation) — not mixed.

## Non-goals
- No change to the honesty signal set (physics/price/brand/reviews) — only the physics
  energy density becomes chemistry-aware.
- No external price source (CamelCamelCamel was removed). Sparkline = our own history.

## Configuration
```
SEARCHES = [
  {"category": "power_bank",    "url": "...battery+bank&s=review-rank"},
  {"category": "power_station", "url": "...lifepo4+power+station"},
]   # extensible; add more {category,url} entries
ENERGY_DENSITY_WH_PER_KG = {"lifepo4": 160, "li-ion": 255, "li-po": 255,
                            "nmc": 250, "default": 255}
```

## Data model (new columns; idempotent ALTER TABLE migration)
- `category` (str): `power_bank` | `power_station`
- `capacity_wh` (float): power banks = `claimed_mah/1000*3.7`; power stations = stated Wh
- `chemistry` (str|null): `lifepo4` | `li-ion` | `li-po` | `nmc`
- `cost_per_wh` (float, derived)
- Power-station feature columns: `ac_output_w`, `ac_sockets`, `solar_input_w`,
  `cycle_life`, `usb_c_pd_w`, `expandable` (bool), `ups` (bool)

## Parsing (`parse.py`, pure + unit-tested)
- `is_power_station(title)` — accept "power station / portable power station / solar
  generator / LiFePO4 … generator"; reject accessories (reuse strong-exclude).
- `extract_chemistry(text)` — lifepo4/lfp/"lithium iron phosphate" → lifepo4;
  lipo/"lithium polymer"/polymer → li-po; nmc/ternary → nmc; "li-ion"/"lithium ion" → li-ion.
- `extract_wh(text)` — `N Wh` / `N kWh`→×1000 (plausible 50–10000 Wh).
- `extract_ac_output_w(text)` — continuous inverter watts ("1000W", "2200W output",
  "pure sine wave 1500W"); ignore "surge/peak".
- `extract_ac_sockets(text)` — count of AC outlets/sockets ("2 AC", "3x socket").
- `extract_solar_input_w(text)` — max solar input watts / MPPT presence.
- `extract_cycle_life(text)` — "3000 cycles" → 3000.
- expandable / UPS via keyword detection.
- USB-C PD watts reuse `extract_watts` (pd_w).

## Scraper
- Loop over `SEARCHES`: paginate each (existing unlimited logic), tag cards with the search's
  category, dedupe by ASIN (first search wins). Per-card relevance uses the search's category
  (`is_battery_bank` vs `is_power_station`).
- Detail scrape extracts the category-relevant fields (power stations: Wh + AC/solar/cycle/
  expandable/UPS; power banks: existing). Incremental/full-refresh and progress unchanged.

## Analysis (`analysis.py`) — per category
- Group products by `category`; compute **within each group**: price-per-(mAh|Wh) outliers,
  hedonic regression, brand reputation.
- `capacity_wh` set for all; `cost_per_wh = price / capacity_wh`. Power banks keep
  `cost_per_mah`.
- **Chemistry-aware physics:** density = `ENERGY_DENSITY_WH_PER_KG[chemistry or default]`;
  `min_weight_g = capacity_wh / density * 1000`; same ratio/flag/overstatement logic on Wh.
- Regression feature sets are **category-specific**:
  - power_bank: capacity, pd_w, usb_c, wireless, display, passthrough, solar, brand_trust, rating
  - power_station: capacity_wh, ac_output_w, ac_sockets, solar_input_w, usb_c_pd_w,
    cycle_life, expandable, ups, brand_trust, rating

## UI (`app.js`, `templates/index.html`)
- **Top toggle**: Power Banks | Power Stations → sets active `category`; table filters to it.
- **Category-specific columns**: banks show mAh + £/10Ah; stations show Wh + £/Wh + AC W.
- **Category-specific FACTORS** (value-to-you sliders) and **filters** (banks: ports/wireless/
  …; stations: AC output, sockets, solar input, cycle life, expandable, UPS).
- **Chemistry filter** dropdown (All / LiFePO4 / Li-ion / Li-po / NMC / Unknown) on both pages.
- Shared across pages: honesty score + tooltip, value-to-you (0–100, normalized within the
  filtered category), avg £, ratings count, price sparkline, CSV export.
- Persisted UI state keyed per category where it differs (factor weights, filters).

## Error handling / edge cases
- Missing Wh on a power station → no cost-per-Wh, ranks last; physics skipped.
- A product matching neither category's relevance is dropped (as today).
- Unknown chemistry → default density (255) for physics; shows "unknown" in filter.

## Testing
- Parsers: is_power_station, extract_chemistry, extract_wh (Wh/kWh), ac_output, ac_sockets,
  solar_input, cycle_life, expandable/ups.
- Chemistry-aware physics: LiFePO4 vs Li-ion thresholds (a LiFePO4 pack that's plausible under
  160 Wh/kg but "impossible" under 260, and vice-versa).
- Per-category analysis on an in-memory mixed catalog: outliers/regression computed within
  category; cost_per_wh correct.
- Serialization includes new fields.

## Build order
1. Backend: config, models+migration, parsers, scraper multi-search/category, analysis
   per-category + chemistry-aware physics + Wh/cost_per_wh.
2. UI: top toggle, category-specific columns/factors/filters, chemistry filter.
Verify each phase (tests + a live spot-check of the power-station search).
