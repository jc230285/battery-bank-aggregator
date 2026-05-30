# Battery Bank Aggregator — Design

**Date:** 2026-05-28
**Status:** Implemented (2026-05-29). See "As-built notes" at the end for how the
implementation evolved beyond this original design.

## Purpose

Scrape Amazon UK battery-bank ("power bank") listings on a 6-hour schedule, catalog them
with structured features, track price/stock over time, and serve a web UI that ranks them
by **cost-per-mAh**, scores how honestly each brand reports capacity, and computes a
**personalized feature value** based on which features the user actually wants.

Seed search (sorted by review rank):
`https://www.amazon.co.uk/s?k=battery+bank&s=review-rank` (top ~50 results).

## Goals

- Catalog the top ~50 results from the seed search.
- Refresh every 6 hours: detect new products, price changes, and stock changes.
- Extract structured features for filtering.
- Rank by cost-per-mAh.
- Flag brands that misreport mAh, via a **weighted, UI-adjustable** honesty score.
- Estimate the market value of features (hedonic regression) and produce a
  **personalized, weighted value-to-you** score from features the user selects.

## Non-Goals

- No physical testing of cells (we infer honesty from listing data + research signals).
- No guarantee every product is captured every run (CAPTCHAs cause graceful partial runs).
- Not a statistically rigorous pricing model — ~50 rows makes the regression *indicative*.
- No multi-search / multi-marketplace support in v1 (single seed search, amazon.co.uk).

## Architecture

Single Flask (Python) application, one process, on a fixed non-dev port **8473**
(configurable via env). An in-process **APScheduler** `BackgroundScheduler` runs the
scrape+analyze job every 6 hours, and once on startup if existing data is older than
6 hours (or absent). Storage is **SQLite** via SQLAlchemy. Scraping uses **Playwright**
(Chromium). After each scrape an analysis pass recomputes derived fields. The web UI is
server-rendered (Jinja) with client-side filtering/sorting/weighting (≤50 rows is trivial
in the browser).

```
+------------------+      every 6h / on stale startup
|   APScheduler    |---------------------------+
+------------------+                            v
                                       +-----------------+
                                       |  Scrape job     |
                                       |  (Playwright)   |
                                       +-----------------+
                                                |
                                writes products + price_history + scrape_runs
                                                v
                                       +-----------------+
                                       |   SQLite (ORM)  |
                                       +-----------------+
                                                ^
                                  reads/writes derived fields
                                                |
+------------------+   HTTP    +-----------------+   +-----------------+
|  Browser (UI)    |<--------->|  Flask routes   |-->|  Analysis pass  |
|  filters/sliders |  JSON/HTML|  (API + pages)  |   | cost/mAh,       |
+------------------+           +-----------------+   | honesty, regr.  |
                                                     +-----------------+
```

## Components

### 1. Scraper (`scraper/`)
- Load the seed search URL; collect top ~50 ASINs + basics: title, price, rating,
  review count, image URL, listing position.
- Visit each product detail page to extract: brand, **weight**, dimensions, claimed mAh,
  USB-A count, USB-C count, max output W, **PD wattage**, wireless (bool), display (bool),
  pass-through charging (bool), solar (bool), and raw bullet/spec text (kept as JSON).
- **Anti-block:** single persistent browser context, randomized 2–6s delays between page
  loads, realistic user agent, `en-GB` locale/currency, persisted cookies. On CAPTCHA or
  block detection, record the run as `partial`, store whatever was collected, and retry on
  the next cycle. Never hammer/retry aggressively within a run.
- **Parsing:** regex + keyword matching over the title, the "About this item" bullets, and
  the "Technical/Product details" table. Per-product specs (especially weight) come only
  from the detail page, which is why detail pages must be visited.

### 2. Data model (SQLite + SQLAlchemy)
- **products**: `asin` (PK), title, brand, url, image_url, first_seen, last_seen, price,
  in_stock, rating, review_count, claimed_mah, weight_g, dims, usb_a, usb_c, max_w, pd_w,
  wireless, display, passthrough, solar, raw_specs (JSON), cost_per_mah, honesty_score,
  honesty_flags (JSON), fair_price, price_delta.
- **price_history**: id, asin (FK), captured_at, price, in_stock. Appended each run so the
  UI can show price/stock trends and "changed since last run".
- **scrape_runs**: id, started_at, finished_at, status (`ok`/`partial`/`failed`), n_found,
  n_errors, notes. Audit trail and basis for staleness check.

### 3. Analysis pass (`analysis/`)
Runs after each scrape, over the full current catalog.

**Cost-per-mAh:** `price / claimed_mah` (only when mAh parsed). Primary default sort.

**Honesty score (weighted, UI-adjustable; 0–100 + flag badges).** Four signals, each
producing a 0–1 sub-score, combined with weights (defaults below, all adjustable live via
UI sliders; physics weighted highest):
1. **Physics (default weight highest):** claimed Wh = `claimed_mah/1000 * 3.7`;
   minimum plausible weight = `claimed_Wh / 260` kg (260 Wh/kg is a generous Li-ion
   ceiling). If listed weight is below that floor, the capacity is physically impossible
   → strong penalty + "impossible capacity" flag.
2. **Price-per-mAh outlier:** z-score of cost_per_mah across the catalog; implausibly cheap
   (e.g. below ~2–3 std devs) → penalty + flag.
3. **Brand trust:** curated good-brand list (Anker, Baseus, INIU, UGREEN, …) → trusted;
   unknown/no-name → "unverified".
4. **Review-text mining:** count fake-capacity phrases ("won't hold charge",
   "not as advertised", "fake mAh", "drains fast") in scraped review snippets; higher
   frequency → penalty.

The backend computes the four sub-scores and stores them; the UI applies the user's
weight sliders to produce the live composite score and ordering. Default weights are
defined server-side and sent to the client.

**Feature value — hedonic regression + personalized weighting.**
- **Global model:** OLS regression of price on features (capacity, pd_w, port counts,
  wireless, display, passthrough, solar, brand-trust, rating) → marginal £-value per
  feature (what the market charges). Reported as *indicative* given the small sample;
  feature set kept lean to avoid overfitting.
- **Personalized value-to-you:** in the UI the user assigns an **importance weight**
  (slider, not just on/off) to each feature they care about.
  `value_to_you(product) = Σ over features the product has ( market_£_value(feature) ×
  user_importance_weight(feature) )`. Features with zero importance contribute nothing.
  Products rank by value-to-you, and an over/under-priced badge compares price to the
  feature-implied fair value. Recomputed live in the browser as sliders move.

### 4. Web UI (`templates/`, `static/`)
- **Sortable table**, default sort cost-per-mAh ascending. Columns: image, title/brand
  (link to Amazon), price, mAh, cost/mAh, feature icons, rating, honesty score + flag
  badges, value-to-you, over/under-priced badge, price sparkline.
- **Filter panel:** brand, capacity range, port types, PD watts, wireless/display/
  passthrough/solar toggles, min rating, honesty threshold, in-stock only.
- **Weight controls:** honesty-signal weight sliders (4) and per-feature importance
  sliders for "features I want". Both recompute scores/order client-side.
- **Status header:** last scrape time, next scheduled run, products tracked, last run
  status (ok/partial).
- **Tech:** Jinja templates + Tailwind (CDN) + small vanilla JS / Alpine.js for
  client-side filter/sort/weight math (≤50 rows = trivial, no heavy framework).

### 5. Scheduling & ops
- `python app.py` starts the web server and the in-process scheduler. On startup, if the
  most recent `ok`/`partial` run is older than the interval (or none), run immediately;
  otherwise schedule the next run on the interval.
- **Config** (env or a config file): seed search URL, top-N (default 50), interval hours
  (default 6), port (default 8473), trusted-brand list, default honesty weights.
- **Logging** to `./logs`; `scrape_runs` is the durable audit trail.

## Data flow (one cycle)
1. Scheduler fires → scrape job opens Playwright, reads search page (top ~50).
2. For each ASIN, visit detail page, parse specs, upsert into `products`, append
   `price_history` row, detect new/changed.
3. Close browser; analysis pass recomputes cost/mAh, honesty sub-scores, regression,
   fair_price.
4. Write `scrape_runs` summary.
5. UI reads current `products`; client applies user weights/filters for live ranking.

## Error handling
- **CAPTCHA / block:** detect (challenge page / missing expected selectors) → stop the run
  early, persist partial data, mark run `partial`, surface in status header, retry next
  cycle.
- **Unparseable fields** (e.g. missing weight or mAh): store nulls; affected signals are
  skipped for that product and it is marked "insufficient data" rather than mis-scored.
- **Scheduler overlap:** guard so a new run cannot start while one is in progress.

## Testing
- **Parsers:** unit tests against saved HTML fixtures of a search page and several product
  pages (incl. edge cases: missing weight, "20000mAh" vs "20,000 mAh", multi-pack).
- **Honesty signals:** unit tests for the physics floor (known impossible vs plausible),
  outlier detection, brand matching, phrase counting.
- **Regression / value-to-you:** unit tests on synthetic catalogs with known coefficients;
  verify personalized weighting math.
- **Staleness/scheduling:** test the "run on stale startup" and overlap-guard logic.
- Scraper integration test gated/manual (hits live Amazon) — not in the default suite.

## Open caveats (accepted)
1. Amazon scraping can hit CAPTCHAs; the app degrades to partial runs by design.
2. The hedonic regression on ~50 products is directional (outlier-spotting), not precise.

---

## As-built notes (2026-05-29)

The implementation diverged from / extended the original design as follows. This section
is the source of truth for current behaviour.

**Scrape scope — now unlimited, not top-50.** `TOP_N=0` (unlimited): the scraper
paginates the search (up to `MAX_PAGES=30`) collecting every relevant card, then scrapes
until a CAPTCHA stops it. Result is typically ~300 products, not 50. The regression
therefore runs on a much larger, more useful sample than the "~50, indicative" caveat.

**Incremental scraping (sustainability).** Specs are static per ASIN, so a normal run
detail-scrapes only *new* products and **price-refreshes known ones from the search card**
(no detail-page hit). New products are scraped first so the catalog visibly grows. A
**"Full refresh"** button (`/api/run?full=1`) forces a full detail re-scrape (e.g. to apply
parser updates). This cut a full run from ~330 requests to ~75 and avoids routine CAPTCHAs.

**Relevance filtering.** `parse.is_battery_bank` keeps power banks and drops accessories
(cases/cables/covers) at card and detail stages; `parse.is_accessory` (strong signals only)
is used for safe deletion of stored rows.

**Honesty score additions.** Beyond the 4 weighted signals: physics now also yields a
**capacity ceiling** (`max_plausible_mah`) and an **overstatement %** for impossible claims
(e.g. "40000mAh @ 313g → max ~22000, +82%"), shown in the badge tooltip. A **confidence
cue** (`n/4`) shows how many signals were available. Implausible stored weights
(<40g / >6kg) are ignored via `_clean_weight`.

**Feature value — NNLS, not plain OLS.** The hedonic model uses non-negative least squares
(`scipy.optimize.nnls`, clipped-OLS fallback flagged in the model) so feature values are
always ≥0. Feature importance defaults to **0** (opt-in): value-to-you is £0 until the user
raises a slider. Default table sort is **rating** (desc).

**Reconciliation — grace-period.** Delisted products are removed only after they've been
absent **`REMOVE_AFTER_HOURS=30`** (not on a single miss, since results shift run-to-run),
and only on a healthy run (≥20 items). Definitive accessories are removed immediately.

**Robustness / correctness (post-review).** SQLite engine uses
`check_same_thread=False` + WAL + busy_timeout for the background-scrape / Flask-read
overlap; client-side rendering HTML-escapes scraped strings and allows only `https` URLs
(XSS); `run_cycle` guarantees lock release + failure logging; orphaned "running" runs are
marked `interrupted` on startup; a parse-yield guard downgrades a run to `partial` if
<50% of items have a price (catches silently-broken selectors); interim analysis is
time-throttled.

**Extras:** client-side **CSV export** of the current filtered/sorted view; live scrape
**progress** + limit stats (`[pages=N scraped=M dur=Ns]`) in the run record and status bar.

**Tests:** 28 tests across `tests/test_{parse,analysis,scraper,pipeline,app}.py` (pure
parsers, analysis pipeline on in-memory DB, scraper helpers, serialization). Run with the
inline runner in CI-less form; note a global `pytest-socket` plugin blocks plain `pytest`.
