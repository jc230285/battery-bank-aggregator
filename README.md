# Battery Aggregator

Scrapes several Amazon UK searches, catalogs the results with structured specs, tracks price
over time, and serves a local web UI that scores how **honestly** each listing reports its
capacity and computes a **personalized "value to you"** from the features you care about.

Two product **categories**, shown as separate pages via a top toggle:
- **Power Banks** — mAh-rated; ranked by **cost-per-mAh**.
- **Power Stations** — Wh-rated (mostly LiFePO4); ranked by **cost-per-Wh**, with
  power-station features (AC output/sockets, solar input, cycle life, expandable, UPS).

A **battery-chemistry filter** (LiFePO4 / Li-ion / Li-polymer / NMC / unknown) is available on
both pages, and the physics honesty-check is **chemistry-aware** (LiFePO4 ~160 vs Li-ion
~255 Wh/kg). Each category has its own metrics, filters, value-factors, and analysis
(price-outliers / regression / brand-reputation are computed within a category, not mixed).

Single self-contained Flask app: web UI + multi-search scraper + an in-process scheduler that
refreshes every 6 hours.

## Quick start

```bash
pip install -r requirements.txt
python -m playwright install chromium    # one-time: download the browser
python app.py
```

Then open **http://127.0.0.1:8473**.

On first run (empty database) it scrapes immediately; afterwards it serves the existing
catalog instantly and refreshes on the 6-hour schedule. Everything is stored in
`battery_banks.sqlite3` next to the code.

## How it works

```
APScheduler (hourly + daily) ─▶ scrape (Playwright) ─▶ SQLite ─▶ analysis ─▶ Flask UI
```

1. **Scrape** — two cadences:
   - **Hourly** — refreshes the **N oldest products by `last_seen`** (default 40) plus every
     watchlist item by visiting `/dp/<asin>` directly. No search pagination. Cheap; one
     CAPTCHA mid-cycle costs at most a single batch.
   - **Daily** — runs a full *discovery* sweep across the configured Amazon UK searches to
     find new ASINs, paginating until exhausted or blocked.
   Either path stops cleanly on CAPTCHA and keeps whatever was collected (a *partial* run).
2. **Store** — upserts into `products`, appends a `price_history` row each run, and records
   each run in `scrape_runs`.
3. **Analyse** — recomputes cost-per-mAh, the honesty score, and the feature-value model.
4. **Serve** — the UI reads the catalog; filtering, sorting and weighting happen live in the
   browser.

### Hourly refresh vs. daily discovery vs. full refresh

The hourly job picks the **N oldest products by `last_seen`** (config: `HOURLY_BATCH_SIZE`)
and visits each detail page — keeps prices fresh on a rolling window without paying for a
full pagination every hour. Daily discovery is the heavy search-pagination flow that finds
new ASINs. The **"Full refresh"** button (or `POST /api/run?full=1`) is the manual lever
that re-scrapes every product's detail page — use it after changing the parsers. The
**Watchlist** page lets you paste any amazon.co.uk URL — those items are always refreshed
on the hourly job, regardless of how recently they were last seen.

## The scores

- **Cost per 10Ah** (`£/10Ah`) — `price ÷ claimed_mAh × 10000`. Lower is better. Primary
  value metric. Note it uses the *claimed* capacity, so check the honesty score alongside.
- **Honesty (0–100, weighted)** — blends four signals, each with a UI-adjustable weight:
  - *Physics* — **chemistry-aware** energy-density ceiling (LiFePO4 ~160, Li-ion ~255 Wh/kg)
    means a claimed capacity implies a minimum weight; a lighter pack is impossible. The
    tooltip shows the **max plausible capacity** and **overstatement %** (e.g. "40000mAh @
    313g → max ~22000, +82%").
  - *Price-per-capacity outlier* — implausibly cheap per claimed mAh/Wh ≈ inflated capacity.
  - *Brand trust* — trusted brands → 1.0; unverified → a data-grounded **brand reputation**
    (volume-weighted, shrinkage-smoothed rating, minus a fake penalty).
  - *Review text* — mentions of "fake capacity", "won't hold charge", etc.
  - A `n/4` cue shows how many signals were available. Impossible-capacity rows are tinted red.
- **Value to you** — a **0–100 fit score**: set an **importance weight (0 = don't care)** per
  factor (incl. cheapness) in the left panel; each product is scored on the weighted average of
  those factors, normalised against the currently-filtered list. It reads `—` until you raise a
  slider. The non-negative regression separately powers the **"vs fair"** market-price badge.

## Web UI

- Sortable table (default sort: rating). Click any column header to sort.
- **Filters**: brand, min/max mAh, min rating, min honesty, in-stock, and feature toggles.
- **Honesty signal weights** and **"Features I want"** importance sliders — all recompute
  live. Filters and weights persist across reloads.
- **Export CSV** — downloads the current filtered/sorted view (with your live weights).
- **Status bar** — product count, last-run status, next scheduled run, and live scrape
  progress.

## Configuration (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `BBA_HOST` | `127.0.0.1` | Bind address (set `0.0.0.0` to expose on your LAN/tailnet) |
| `BBA_PORT` | `8473` | Port |
| `BBA_SEARCH_URL` | battery-bank search | Amazon UK search to scrape |
| `BBA_TOP_N` | `0` | Max products (`0` = unlimited, until CAPTCHA/end) |
| `BBA_MAX_PAGES` | `30` | Pagination safety cap |
| `BBA_INTERVAL_HOURS` | `1` | Hourly-refresh interval (the oldest-N detail scrape) |
| `BBA_HOURLY_BATCH_SIZE` | `40` | How many oldest products refresh per hourly cycle |
| `BBA_DISCOVERY_INTERVAL_HOURS` | `24` | Daily discovery (full search pagination) interval |
| `BBA_REMOVE_AFTER_HOURS` | `30` | Drop a product only after it's been absent this long |
| `BBA_CACHE_DIR` | `./cache` | Where the HTML cache lives (mount a volume in containers) |
| `BBA_CACHE_TTL_HOURS` | `336` | Detail-page HTML cache TTL (14 days) |
| `BBA_STATE_DIR` | `./state` | Persistent browser storage_state (cookies for stealth) |
| `BBA_LOG_DIR` | `./logs` | Log directory |
| `BBA_HEADLESS` | `1` | `0` to watch the browser |
| `BBA_MIN_DELAY_S` / `BBA_MAX_DELAY_S` | `2` / `6` | Random delay between page loads |
| `BBA_DB` | `battery_banks.sqlite3` | SQLite path |

Trusted brands, fake-review phrases, and default honesty weights live in `config.py`.

## API

- `GET /` — the web UI
- `GET /api/products` — full catalog as JSON
- `GET /api/status` — counts, last run, per-job next runs (`hourly`/`discovery`), live progress
- `POST /api/run` — manually trigger the **discovery** sweep (`?full=1` re-scrapes every
  detail page). The hourly refresh runs automatically every `BBA_INTERVAL_HOURS`.
- `POST /api/watchlist` `{"url": "https://www.amazon.co.uk/dp/..."}` — add a custom product
  to the watchlist; returns the parsed ASIN and queues a background scrape
- `DELETE /api/watchlist/<asin>` — remove a watchlist item

## Layout

| File | Purpose |
|---|---|
| `app.py` | Flask routes, scheduler, `run_cycle` orchestration, reconciliation |
| `scraper.py` | Playwright scraping, pagination, incremental refresh |
| `parse.py` | Pure parsers: mAh, weight, watts, ports, brand, relevance |
| `analysis.py` | Cost-per-mAh, honesty signals, NNLS feature-value model |
| `models.py` | SQLAlchemy models (`Product`, `PriceHistory`, `ScrapeRun`, `Meta`) |
| `templates/`, `static/` | Jinja page + Tailwind/vanilla-JS front end |
| `tests/` | Unit + pipeline tests |
| `docs/superpowers/specs/` | Original design doc + as-built notes |

## Tests

```bash
python -m pytest tests/      # if your environment allows it
```

A global `pytest-socket` plugin may block plain `pytest`; the tests are pure (no network),
so you can also run them directly:

```bash
python - <<'PY'
import importlib, glob, os, sys
sys.path.insert(0, "tests")
for m in (os.path.splitext(os.path.basename(f))[0] for f in glob.glob("tests/test_*.py")):
    mod = importlib.import_module(m)
    for n in dir(mod):
        if n.startswith("test_"): getattr(mod, n)()
print("ok")
PY
```

## Caveats

- Scraping Amazon is against their ToS and unreliable by nature — runs can hit CAPTCHAs and
  finish *partial*. The app degrades gracefully and tracks where it stopped.
- The feature-value regression is **indicative**, not precise — good for spotting
  over/under-priced outliers, not exact pricing.
- Honesty signals are inferred from listing data (no physical testing); ~half of cheap
  listings omit weight, so the physics check is skipped for them.
