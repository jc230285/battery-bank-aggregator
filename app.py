"""Battery Bank Aggregator: Flask UI + in-process 6h scraper/scheduler."""
import logging
import os
import threading
import time
import datetime

from flask import Flask, jsonify, render_template, request
from sqlalchemy import func
from apscheduler.schedulers.background import BackgroundScheduler

import config
import analysis
import parse
import scraper
from models import Product, PriceHistory, ScrapeRun, Meta, SessionLocal, init_db, utcnow

os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(config.LOG_DIR, "app.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("app")

app = Flask(__name__)
_run_lock = threading.Lock()
_progress = {"phase": "idle", "done": 0, "total": 0}


def _upsert(session, item):
    p = session.get(Product, item["asin"])
    now = utcnow()
    if p is None:
        p = Product(asin=item["asin"], first_seen=now)
        session.add(p)
    for field in (
        "title", "brand", "url", "image_url", "category", "price", "in_stock",
        "rating", "review_count", "claimed_mah", "capacity_wh", "chemistry",
        "weight_g", "date_first_available", "usb_a", "usb_c", "max_w", "pd_w",
        "wireless", "display", "passthrough", "solar", "ac_output_w", "ac_sockets",
        "solar_input_w", "cycle_life", "expandable", "ups",
        "raw_specs", "review_snippets", "listing_position",
    ):
        if field in item and item[field] is not None:
            setattr(p, field, item[field])
    p.last_seen = now
    # If a previously-delisted product reappears, un-delist it.
    if p.delisted_at is not None:
        p.delisted_at = None
    session.add(PriceHistory(
        asin=p.asin, captured_at=now, price=item.get("price"),
        in_stock=item.get("in_stock", True)))
    return p


def run_cycle(trigger="manual", full=False):
    """Scrape -> persist -> analyse. Guarded so runs never overlap.

    full=True re-scrapes every product's detail page; otherwise known products
    with specs are price-refreshed from the search card (fast, low block risk).
    """
    if not _run_lock.acquire(blocking=False):
        log.info("run skipped (%s): another run in progress", trigger)
        return {"skipped": True}
    session = None
    run = None
    processed = [0]
    last_analysis = [0.0]

    def on_item(item):
        """Persist one product immediately with partial scores so rows appear live."""
        p = _upsert(session, item)
        p.cost_per_mah = analysis.cost_per_mah(p.price, p.claimed_mah)
        phys, f_phys = analysis.physics_subscore(
            analysis.capacity_wh_of(p), p.weight_g, p.chemistry)
        brand, f_brand = analysis.brand_subscore(p.brand)
        rev, f_rev = analysis.reviews_subscore(p.review_snippets)
        p.honesty = {"physics": phys, "price": None, "brand": brand, "reviews": rev}
        p.honesty_flags = [f for f in (f_phys, f_brand, f_rev) if f]
        session.commit()
        # Periodically recompute catalog-wide metrics (price outliers, regression,
        # feature value) so value-to-you stays current — but throttle by time so a
        # fast card-only refresh burst doesn't trigger dozens of redundant passes.
        processed[0] += 1
        if processed[0] % 8 == 0 and time.time() - last_analysis[0] > 10:
            try:
                analysis.run_analysis(session)
                last_analysis[0] = time.time()
            except Exception as e:  # noqa: BLE001 - interim analysis must not abort scrape
                log.warning("interim analysis failed: %s", e)

    def on_progress(phase, done, total):
        _progress.update(phase=phase, done=done, total=total)

    try:
        log.info("scrape cycle starting (%s, full=%s)", trigger, full)
        _progress.update(phase="starting", done=0, total=config.TOP_N)
        session = SessionLocal()
        run = ScrapeRun(trigger=trigger, started_at=utcnow(), status="running")
        session.add(run)
        session.commit()
        known = {a for (a,) in session.query(Product.asin).all()}
        detailed = {a for (a,) in
                    session.query(Product.asin).filter(
                        (Product.claimed_mah.isnot(None)) | (Product.capacity_wh.isnot(None))).all()}
        cat_counts = dict(session.query(Product.category, func.count())
                          .group_by(Product.category).all())
        # Watchlist items are user-chosen URLs — always include them in the
        # detail-scrape queue and always fetch a fresh detail (excluded from the
        # detailed_asins skip-set), so their price/avg stay current.
        watchlist = {a for (a,) in session.query(Product.asin)
                     .filter(Product.category == "watchlist").all()}
        detailed = detailed - watchlist
        items, status, notes = scraper.scrape(
            on_item=on_item, on_progress=on_progress, known_asins=known,
            detailed_asins=detailed, full=full, category_counts=cat_counts,
            watchlist_asins=watchlist)
        _progress.update(phase="reconciling", done=len(items), total=len(items))

        # Reconcile the catalog. Accessories (strong signal of mis-ingest) are
        # still hard-deleted, but products that simply haven't appeared for a
        # while are soft-delisted — kept in the DB for price history, excluded
        # from the hourly queue, hidden from the UI by default. Skip the sweep
        # entirely on an unhealthy run so a blocked scrape can't mass-delist
        # the catalog. Products seen again in this run have their delisted_at
        # cleared (handled in _upsert).
        now = utcnow()
        healthy = status != "failed" and len(items) >= 20
        cutoff = datetime.timedelta(hours=config.REMOVE_AFTER_HOURS)
        removed = 0
        delisted = 0
        for p in session.query(Product).all():
            if parse.is_accessory(p.title):
                session.delete(p)
                removed += 1
                continue
            if (healthy and p.delisted_at is None and p.last_seen
                    and (now - p.last_seen) > cutoff):
                p.delisted_at = now
                delisted += 1
        if removed or delisted:
            session.commit()
            log.info("discovery: removed %d accessories, soft-delisted %d stale (>%dh absent)",
                     removed, delisted, config.REMOVE_AFTER_HOURS)

        analysis.run_analysis(session)  # finalize: price outliers + regression
        run.status = status
        run.n_found = len(items)
        run.notes = (notes + (f" removed {removed}; delisted {delisted};"
                              if (removed or delisted) else "")).strip()
        run.finished_at = utcnow()
        session.commit()
        log.info("scrape cycle done (%s): %d items, %d removed, %d delisted, status=%s",
                 trigger, len(items), removed, delisted, status)
        return {"status": status, "n_found": len(items),
                "removed": removed, "delisted": delisted, "notes": run.notes}
    except Exception as e:  # noqa: BLE001 - record failure, keep server alive
        log.exception("scrape cycle failed (%s)", trigger)
        if session is not None:
            try:
                session.rollback()
                if run is not None:
                    run.status = "failed"
                    run.notes = f"{type(e).__name__}: {e}"
                    run.finished_at = utcnow()
                    session.add(run)
                    session.commit()
            except Exception:  # noqa: BLE001 - don't mask the original failure
                log.exception("failed to record run failure")
        return {"status": "failed", "error": str(e)}
    finally:
        _progress.update(phase="idle", done=0, total=0)
        if session is not None:
            session.close()
        _run_lock.release()


def _last_run(session):
    return (session.query(ScrapeRun)
            .filter(ScrapeRun.status != "running")
            .order_by(ScrapeRun.started_at.desc()).first())


def _data_is_stale():
    session = SessionLocal()
    try:
        last = _last_run(session)
        return (last is None or last.finished_at is None or
                (utcnow() - last.finished_at) >
                datetime.timedelta(hours=config.INTERVAL_HOURS))
    finally:
        session.close()


def _product_dict(p):
    hist_prices = [h.price for h in p.history if h.price is not None]
    avg_price = round(sum(hist_prices) / len(hist_prices), 2) if hist_prices else None
    return {
        "asin": p.asin, "title": p.title, "brand": p.brand, "url": p.url,
        "image_url": p.image_url, "category": p.category or "power_bank",
        "price": p.price, "in_stock": p.in_stock,
        "rating": p.rating, "review_count": p.review_count,
        "claimed_mah": p.claimed_mah, "capacity_wh": p.capacity_wh,
        "chemistry": p.chemistry, "weight_g": p.weight_g,
        "date_first_available": p.date_first_available, "avg_price": avg_price,
        "usb_a": p.usb_a, "usb_c": p.usb_c, "max_w": p.max_w, "pd_w": p.pd_w,
        "wireless": p.wireless, "display": p.display,
        "passthrough": p.passthrough, "solar": p.solar,
        "ac_output_w": p.ac_output_w, "ac_sockets": p.ac_sockets,
        "solar_input_w": p.solar_input_w, "cycle_life": p.cycle_life,
        "expandable": p.expandable, "ups": p.ups,
        "cost_per_mah": p.cost_per_mah, "cost_per_wh": p.cost_per_wh,
        "honesty": p.honesty or {},
        "honesty_flags": p.honesty_flags or [], "fair_price": p.fair_price,
        "price_delta": p.price_delta, "feature_contrib": p.feature_contrib or {},
        "first_seen": p.first_seen.isoformat() if p.first_seen else None,
        "last_seen": p.last_seen.isoformat() if p.last_seen else None,
        "delisted_at": p.delisted_at.isoformat() if p.delisted_at else None,
        "history": [{"t": h.captured_at.isoformat(), "price": h.price}
                    for h in p.history if h.price is not None],
    }


def _status(session):
    last = _last_run(session)
    next_runs = {}
    if scheduler.running:
        for j in scheduler.get_jobs():
            if j.next_run_time:
                next_runs[j.id] = j.next_run_time.isoformat()
    next_run = min(next_runs.values()) if next_runs else None
    cooldown = _captcha_cooldown_until(session)
    return {
        "count": session.query(Product).count(),
        "last_run": ({"status": last.status, "trigger": last.trigger,
                      "n_found": last.n_found, "notes": last.notes,
                      "finished_at": last.finished_at.isoformat() if last.finished_at else None}
                     if last else None),
        "next_run": next_run,
        "next_runs": next_runs,  # per-job (hourly, discovery)
        "captcha_cooldown_until": cooldown.isoformat() if cooldown else None,
        "running": _run_lock.locked(),
        "progress": dict(_progress),
    }


@app.route("/")
def index():
    session = SessionLocal()
    try:
        products = session.query(Product).all()
        data = {
            "products": [_product_dict(p) for p in products],
            "model": (session.get(Meta, "feature_model").value
                      if session.get(Meta, "feature_model") else {}),
            "honesty_weights": config.DEFAULT_HONESTY_WEIGHTS,
            "status": _status(session),
        }
        return render_template("index.html", data=data)
    finally:
        session.close()


@app.route("/api/products")
def api_products():
    session = SessionLocal()
    try:
        return jsonify([_product_dict(p) for p in session.query(Product).all()])
    finally:
        session.close()


@app.route("/api/status")
def api_status():
    session = SessionLocal()
    try:
        return jsonify(_status(session))
    finally:
        session.close()


@app.route("/api/run", methods=["POST"])
def api_run():
    if _run_lock.locked():
        return jsonify({"started": False, "reason": "already running"}), 409
    full = request.args.get("full", "").lower() in ("1", "true", "yes")
    threading.Thread(target=run_cycle, kwargs={"trigger": "manual", "full": full},
                     daemon=True).start()
    return jsonify({"started": True, "full": full})


def _captcha_cooldown_until(session):
    """Last 'partial' run note containing 'CAPTCHA' marks an IP-reputation
    problem; we hold off any new scrapes for CAPTCHA_BACKOFF_HOURS after it,
    because every additional bot-shaped request during that window worsens
    the IP's standing without actually fetching anything."""
    last = (session.query(ScrapeRun)
            .filter(ScrapeRun.status.in_(("partial", "failed")))
            .order_by(ScrapeRun.started_at.desc()).first())
    if not last or not last.notes or "CAPTCHA" not in last.notes:
        return None
    if not last.finished_at:
        return None
    cool = last.finished_at + datetime.timedelta(hours=config.CAPTCHA_BACKOFF_HOURS)
    return cool if cool > utcnow() else None


def run_hourly_refresh(trigger="hourly"):
    """Hourly cadence: refresh the N oldest products by last_seen + all
    watchlist items via direct detail-page visits (no search pagination).
    Runs a delisting sweep afterwards (now ~hourly instead of every 6 h).

    Honours a CAPTCHA backoff — after Amazon serves a challenge, we sit
    quiet for CAPTCHA_BACKOFF_HOURS rather than re-triggering it."""
    if not _run_lock.acquire(blocking=False):
        log.info("hourly refresh skipped (%s): another run in progress", trigger)
        return {"skipped": True}
    cooldown = None
    cs = SessionLocal()
    try:
        cooldown = _captcha_cooldown_until(cs)
    finally:
        cs.close()
    if cooldown is not None:
        log.info("hourly refresh skipped (%s): CAPTCHA cooldown until %s",
                 trigger, cooldown.isoformat())
        _run_lock.release()
        return {"skipped": True, "cooldown_until": cooldown.isoformat()}
    session = None
    run = None
    try:
        log.info("hourly refresh starting (%s)", trigger)
        _progress.update(phase="starting", done=0, total=config.HOURLY_BATCH_SIZE)
        session = SessionLocal()
        run = ScrapeRun(trigger=trigger, started_at=utcnow(), status="running")
        session.add(run)
        session.commit()

        # Queue = oldest live products (delisted ones are kept in the DB for
        # price history but skipped here so we don't burn cycles checking dead
        # listings). Watchlist items are always in the queue.
        oldest = (session.query(Product.asin)
                  .filter(Product.delisted_at.is_(None))
                  .order_by(Product.last_seen.asc())
                  .limit(config.HOURLY_BATCH_SIZE).all())
        watchlist = (session.query(Product.asin)
                     .filter(Product.category == "watchlist").all())
        asins = list({a for (a,) in oldest} | {a for (a,) in watchlist})

        def on_item(item):
            _upsert(session, item)
            session.commit()

        def on_progress(phase, done, total):
            _progress.update(phase=phase, done=done, total=total)

        def on_delisted(asin):
            p = session.get(Product, asin)
            if p and p.delisted_at is None:
                p.delisted_at = utcnow()
                session.commit()
                log.info("hourly: %s returned 404 — marked delisted", asin)

        # HTTP + BS4: no browser fingerprint to detect, much cheaper than
        # Playwright. The Playwright path stays available as scraper.refresh_asins
        # for cases that need JS-rendered content.
        items, status, notes = scraper.refresh_asins_http(
            asins, on_item=on_item, on_progress=on_progress, on_delisted=on_delisted)
        _progress.update(phase="reconciling", done=len(items), total=len(items))

        # Soft-delist sweep: anything we touched-but-got-back-404 was already
        # marked delisted by refresh_asins_http. Here we also catch the silent
        # case — products absent from results for > REMOVE_AFTER_HOURS get a
        # delisted_at stamp so the next hourly queue skips them. Real
        # accessories still get hard-deleted (they should never have been in
        # the catalog).
        now = utcnow()
        healthy = status != "failed"
        cutoff = datetime.timedelta(hours=config.REMOVE_AFTER_HOURS)
        removed = 0
        delisted = 0
        for p in session.query(Product).all():
            if parse.is_accessory(p.title):
                session.delete(p)
                removed += 1
                continue
            if (healthy and p.delisted_at is None and p.last_seen
                    and (now - p.last_seen) > cutoff):
                p.delisted_at = now
                delisted += 1
        if removed or delisted:
            session.commit()
            log.info("hourly: removed %d accessories, soft-delisted %d stale", removed, delisted)

        analysis.run_analysis(session)
        run.status = status
        run.n_found = len(items)
        run.notes = (notes + (f" removed {removed}; delisted {delisted};"
                              if (removed or delisted) else "")).strip()
        run.finished_at = utcnow()
        session.commit()
        log.info("hourly refresh done (%s): %d/%d items, removed %d, delisted %d, status=%s",
                 trigger, len(items), len(asins), removed, delisted, status)
        return {"status": status, "refreshed": len(items),
                "removed": removed, "delisted": delisted}
    except Exception as e:  # noqa: BLE001
        log.exception("hourly refresh failed (%s)", trigger)
        if session is not None:
            try:
                session.rollback()
                if run is not None:
                    run.status = "failed"
                    run.notes = f"{type(e).__name__}: {e}"
                    run.finished_at = utcnow()
                    session.add(run)
                    session.commit()
            except Exception:  # noqa: BLE001
                log.exception("failed to record hourly failure")
        return {"status": "failed", "error": str(e)}
    finally:
        _progress.update(phase="idle", done=0, total=0)
        if session is not None:
            session.close()
        _run_lock.release()


def _watchlist_scrape_one(asin):
    """One-shot detail scrape for a newly-added watchlist ASIN via the HTTP
    path (same as the hourly refresh). Respects CAPTCHA cooldown so a user
    add during a bad-IP window doesn't make things worse."""
    if not _run_lock.acquire(blocking=False):
        log.info("watchlist scrape skipped (%s): another run in progress", asin)
        return
    cs = SessionLocal()
    try:
        cooldown = _captcha_cooldown_until(cs)
    finally:
        cs.close()
    if cooldown is not None:
        log.info("watchlist scrape skipped (%s): CAPTCHA cooldown until %s",
                 asin, cooldown.isoformat())
        _run_lock.release()
        return
    sess = None
    try:
        sess = SessionLocal()

        def on_item(item):
            p = _upsert(sess, item)
            p.category = "watchlist"
            sess.commit()

        items, status, notes = scraper.refresh_asins_http([asin], on_item=on_item)
        if items:
            analysis.run_analysis(sess)
        else:
            log.warning("watchlist scrape yielded nothing for %s: %s (%s)", asin, notes, status)
    except Exception as e:  # noqa: BLE001 - keep server alive on user-input scrape failure
        log.warning("watchlist scrape failed for %s: %s", asin, e)
    finally:
        if sess is not None:
            sess.close()
        _run_lock.release()


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    """Add an Amazon product URL to the watchlist. Creates a placeholder Product
    row immediately and kicks off a background detail scrape."""
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    asin = parse.asin_from_url(url)
    if not asin:
        return jsonify({"error": "could not parse ASIN from URL"}), 400
    sess = SessionLocal()
    try:
        p = sess.get(Product, asin)
        now = utcnow()
        if p is None:
            p = Product(asin=asin, category="watchlist",
                        url=f"https://www.amazon.co.uk/dp/{asin}",
                        first_seen=now, last_seen=now)
            sess.add(p)
        else:
            p.category = "watchlist"
            p.last_seen = now
        sess.commit()
    finally:
        sess.close()
    threading.Thread(target=_watchlist_scrape_one, args=(asin,), daemon=True).start()
    return jsonify({"asin": asin, "queued": True})


@app.route("/api/watchlist/<asin>", methods=["DELETE"])
def api_watchlist_remove(asin):
    sess = SessionLocal()
    try:
        p = sess.get(Product, asin)
        if p is None or p.category != "watchlist":
            return jsonify({"removed": False}), 404
        sess.delete(p)
        sess.commit()
        return jsonify({"removed": True})
    finally:
        sess.close()


scheduler = BackgroundScheduler(daemon=True)


def _clear_orphan_runs():
    """Runs don't survive a process restart (the lock is in-memory), so any row
    still marked 'running' at startup was orphaned by a killed process."""
    session = SessionLocal()
    try:
        orphans = session.query(ScrapeRun).filter(ScrapeRun.status == "running").all()
        for r in orphans:
            r.status = "interrupted"
            r.finished_at = r.finished_at or utcnow()
        if orphans:
            session.commit()
            log.info("marked %d orphaned run(s) as interrupted", len(orphans))
    finally:
        session.close()


def main():
    init_db()
    _clear_orphan_runs()
    # Two cadences:
    #   - hourly refresh of the oldest N products + all watchlist items
    #     (direct detail pages, no search pagination)
    #   - daily discovery sweep across the configured searches to find new ASINs
    # If we've never run a discovery, fire one immediately; otherwise the
    # hourly job starts the rotation and discovery waits its full interval.
    now = datetime.datetime.now()
    stale = _data_is_stale()
    discovery_first = now if stale else now + datetime.timedelta(hours=config.DISCOVERY_INTERVAL_HOURS)
    hourly_first = now + datetime.timedelta(minutes=5 if stale else 0)
    scheduler.add_job(
        run_hourly_refresh, "interval", hours=config.INTERVAL_HOURS,
        kwargs={"trigger": "hourly"}, id="hourly", max_instances=1,
        coalesce=True, next_run_time=hourly_first,
    )
    scheduler.add_job(
        run_cycle, "interval", hours=config.DISCOVERY_INTERVAL_HOURS,
        kwargs={"trigger": "discovery"}, id="discovery", max_instances=1,
        coalesce=True, next_run_time=discovery_first,
    )
    scheduler.start()
    log.info("serving on http://%s:%d (discovery first: %s, hourly first: %s)",
             config.HOST, config.PORT, discovery_first.isoformat(), hourly_first.isoformat())
    app.run(host=config.HOST, port=config.PORT, debug=False,
            use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
