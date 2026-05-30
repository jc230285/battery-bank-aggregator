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
APScheduler (every 6h) ─▶ scrape (Playwright) ─▶ SQLite ─▶ analysis ─▶ Flask UI
```

1. **Scrape** — paginates the Amazon search (`SEARCH_URL`), keeping only real power banks
   (accessories like cases/cables are filtered out), then visits product pages for specs.
   Scrapes until a CAPTCHA stops it, keeping whatever was collected (a *partial* run).
2. **Store** — upserts into `products`, appends a `price_history` row each run, and records
   each run in `scrape_runs`.
3. **Analyse** — recomputes cost-per-mAh, the honesty score, and the feature-value model.
4. **Serve** — the UI reads the catalog; filtering, sorting and weighting happen live in the
   browser.

### Incremental vs. full refresh

Specs (mAh, weight, ports) don't change for a given product, so a **normal run only
detail-scrapes *new* products** and price-refreshes known ones from the cheap search-result
cards — this keeps routine runs fast and avoids hammering Amazon. The **"Full refresh"**
button (or `POST /api/run?full=1`) re-scrapes every product's detail page (use it after
changing the parsers). "Run scrape now" does a normal incremental run.

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
| `BBA_INTERVAL_HOURS` | `6` | Scrape interval |
| `BBA_REMOVE_AFTER_HOURS` | `30` | Drop a product only after it's been absent this long |
| `BBA_HEADLESS` | `1` | `0` to watch the browser |
| `BBA_MIN_DELAY_S` / `BBA_MAX_DELAY_S` | `2` / `6` | Random delay between page loads |
| `BBA_DB` | `battery_banks.sqlite3` | SQLite path |

Trusted brands, fake-review phrases, and default honesty weights live in `config.py`.

## API

- `GET /` — the web UI
- `GET /api/products` — full catalog as JSON
- `GET /api/status` — counts, last run, next run, live progress
- `POST /api/run` — start an incremental scrape (`?full=1` to re-scrape all detail pages)

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
