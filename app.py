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
        items, status, notes = scraper.scrape(
            on_item=on_item, on_progress=on_progress, known_asins=known,
            detailed_asins=detailed, full=full, category_counts=cat_counts)
        _progress.update(phase="reconciling", done=len(items), total=len(items))

        # Reconcile the catalog: always drop non-battery items; drop delisted ones
        # only after they've been absent for REMOVE_AFTER_HOURS (not on a single
        # miss, since Amazon's result order shifts run to run). Skip the delisting
        # sweep on an unhealthy run so a blocked scrape can't wipe the catalog.
        now = utcnow()
        healthy = status != "failed" and len(items) >= 20
        cutoff = datetime.timedelta(hours=config.REMOVE_AFTER_HOURS)
        removed = 0
        for p in session.query(Product).all():
            # Only delete definitive accessories (strong signal), never borderline
            # items — they already passed the relevance gate at ingest.
            drop = parse.is_accessory(p.title)
            if healthy and p.last_seen and (now - p.last_seen) > cutoff:
                drop = True  # absent from results long enough to be considered delisted
            if drop:
                session.delete(p)
                removed += 1
        if removed:
            session.commit()
            log.info("removed %d products (non-battery or delisted >%dh)",
                     removed, config.REMOVE_AFTER_HOURS)

        analysis.run_analysis(session)  # finalize: price outliers + regression
        run.status = status
        run.n_found = len(items)
        run.notes = (notes + (f" removed {removed};" if removed else "")).strip()
        run.finished_at = utcnow()
        session.commit()
        log.info("scrape cycle done (%s): %d items, %d removed, status=%s",
                 trigger, len(items), removed, status)
        return {"status": status, "n_found": len(items), "removed": removed, "notes": run.notes}
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
        "history": [{"t": h.captured_at.isoformat(), "price": h.price}
                    for h in p.history if h.price is not None],
    }


def _status(session):
    last = _last_run(session)
    next_run = None
    if scheduler.running:
        jobs = scheduler.get_jobs()
        if jobs and jobs[0].next_run_time:
            next_run = jobs[0].next_run_time.isoformat()
    return {
        "count": session.query(Product).count(),
        "last_run": ({"status": last.status, "trigger": last.trigger,
                      "n_found": last.n_found, "notes": last.notes,
                      "finished_at": last.finished_at.isoformat() if last.finished_at else None}
                     if last else None),
        "next_run": next_run,
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
    # If data is missing or older than the interval, run immediately; else first
    # run is one interval out. Then it repeats every INTERVAL_HOURS.
    stale = _data_is_stale()
    first_run = datetime.datetime.now()
    if not stale:
        first_run += datetime.timedelta(hours=config.INTERVAL_HOURS)
    scheduler.add_job(
        run_cycle, "interval", hours=config.INTERVAL_HOURS,
        kwargs={"trigger": "schedule"}, id="scrape", max_instances=1,
        coalesce=True, next_run_time=first_run,
    )
    scheduler.start()
    log.info("serving on http://%s:%d (first scrape: %s)",
             config.HOST, config.PORT, "now" if stale else first_run.isoformat())
    app.run(host=config.HOST, port=config.PORT, debug=False,
            use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
