"""Tests for app-level serialization the UI depends on (no server/DB needed)."""
import contextlib
import os
import sys
import json
import datetime
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import models


@contextlib.contextmanager
def _isolated_db(**env_overrides):
    """Temp-file SQLite + reloaded modules so tests that mutate the DB don't
    share state. (`:memory:` is unsafe across pool connections — separate DBs.)"""
    saved_env = os.environ.copy()
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        os.environ["BBA_DB"] = path
        os.environ.update(env_overrides)
        import importlib
        import config
        importlib.reload(config)
        importlib.reload(models)
        importlib.reload(app)
        models.init_db()
        yield
    finally:
        os.environ.clear(); os.environ.update(saved_env)
        import importlib, config
        importlib.reload(config)
        importlib.reload(models)
        importlib.reload(app)
        try:
            os.unlink(path)
        except OSError:
            pass


def test_product_dict_serialization():
    p = models.Product(
        asin="B0X", title="Power Bank 20000mAh", brand="Anker",
        url="https://www.amazon.co.uk/dp/B0X", image_url="https://img/x.jpg",
        price=29.99, in_stock=True, rating=4.5, review_count=1200,
        claimed_mah=20000, weight_g=350.0, usb_a=1, usb_c=2, max_w=22.5, pd_w=22.5,
        wireless=True, display=False, passthrough=False, solar=False,
        cost_per_mah=29.99 / 20000,
        honesty={"physics": 1.0, "price": 1.0, "brand": 1.0, "reviews": None},
        honesty_flags=[], fair_price=27.5, price_delta=2.49,
        feature_contrib={"capacity": 5.0, "wireless": 7.49},
        first_seen=datetime.datetime(2026, 5, 29, 0, 0, 0),
    )
    d = app._product_dict(p)

    assert d["asin"] == "B0X"
    assert d["price"] == 29.99 and d["wireless"] is True
    assert d["honesty"]["physics"] == 1.0
    assert d["feature_contrib"] == {"capacity": 5.0, "wireless": 7.49}
    assert d["history"] == []  # transient object -> no price-history rows
    assert d["first_seen"].startswith("2026-05-29")
    # The UI embeds this via |tojson, so it must be JSON-serializable.
    json.dumps(d)


def test_product_dict_handles_nulls():
    # A freshly-ingested row with only an asin must still serialize cleanly.
    d = app._product_dict(models.Product(asin="B0NULL"))
    assert d["asin"] == "B0NULL"
    assert d["honesty"] == {} and d["honesty_flags"] == [] and d["feature_contrib"] == {}
    assert d["first_seen"] is None and d["history"] == []
    assert d["avg_price"] is None and d["date_first_available"] is None
    json.dumps(d)


def test_product_dict_avg_price():
    now = datetime.datetime(2026, 5, 29, 12, 0, 0)
    p = models.Product(asin="B0AVG", price=20.0, date_first_available="2024-01-15")
    p.history = [
        models.PriceHistory(price=18.0, captured_at=now),
        models.PriceHistory(price=22.0, captured_at=now),
        models.PriceHistory(price=None, captured_at=now),  # nulls ignored
    ]
    d = app._product_dict(p)
    assert d["avg_price"] == 20.0          # mean of 18 and 22, nulls skipped
    assert d["date_first_available"] == "2024-01-15"


def test_product_dict_surfaces_delisted_at_and_last_seen():
    # The UI uses these to hide delisted rows by default and to schedule
    # the oldest-first refresh queue.
    t = datetime.datetime(2026, 5, 29, 12, 0, 0)
    p = models.Product(asin="B0GONE", last_seen=t, delisted_at=t)
    d = app._product_dict(p)
    assert d["last_seen"].startswith("2026-05-29")
    assert d["delisted_at"].startswith("2026-05-29")
    # A live product carries None.
    live = models.Product(asin="B0LIVE", last_seen=t)
    assert app._product_dict(live)["delisted_at"] is None


def test_product_dict_timestamps_are_marked_utc():
    # utcnow() returns naive datetimes; without an explicit 'Z' the browser
    # parses them as local time and times are silently off by the TZ offset.
    t = datetime.datetime(2026, 5, 29, 12, 34, 56)
    p = models.Product(asin="B0TZ", first_seen=t, last_seen=t, delisted_at=t)
    p.history = [models.PriceHistory(price=10.0, captured_at=t)]
    d = app._product_dict(p)
    assert d["first_seen"].endswith("Z")
    assert d["last_seen"].endswith("Z")
    assert d["delisted_at"].endswith("Z")
    assert d["history"][0]["t"].endswith("Z")


def test_upsert_clears_delisted_at_on_reappearance():
    # When a previously-delisted ASIN shows up again, the row is un-delisted
    # so it rejoins the live queue.
    with _isolated_db():
        sess = models.SessionLocal()
        try:
            sess.add(models.Product(asin="B0BACK", title="Bank",
                                    first_seen=datetime.datetime.utcnow(),
                                    last_seen=datetime.datetime.utcnow(),
                                    delisted_at=datetime.datetime.utcnow()))
            sess.commit()
            assert sess.get(models.Product, "B0BACK").delisted_at is not None
            app._upsert(sess, {"asin": "B0BACK", "title": "Bank", "price": 20.0,
                               "in_stock": True})
            sess.commit()
            assert sess.get(models.Product, "B0BACK").delisted_at is None
        finally:
            sess.close()


def test_product_dict_all_time_low_high():
    now = datetime.datetime(2026, 5, 29, 12, 0, 0)
    p = models.Product(asin="B0ATL", price=19.0)
    p.history = [
        models.PriceHistory(price=25.0, captured_at=now),
        models.PriceHistory(price=18.0, captured_at=now),
        models.PriceHistory(price=22.0, captured_at=now),
        models.PriceHistory(price=None, captured_at=now),  # nulls ignored
    ]
    d = app._product_dict(p)
    assert d["all_time_low"] == 18.0
    assert d["all_time_high"] == 25.0
    # Product with no price history has None for both.
    p2 = models.Product(asin="B0NOATL", price=20.0)
    d2 = app._product_dict(p2)
    assert d2["all_time_low"] is None and d2["all_time_high"] is None


def test_captcha_cooldown_until_when_recent_captcha():
    with _isolated_db(BBA_CAPTCHA_BACKOFF_HOURS="6"):
        sess = models.SessionLocal()
        try:
            # Fresh CAPTCHA 1 minute ago -> we should be in cooldown.
            recent = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
            sess.add(models.ScrapeRun(
                trigger="hourly", started_at=recent, finished_at=recent,
                status="partial", n_found=0,
                notes="CAPTCHA at 0/40; [http-refresh ...]"))
            sess.commit()
            cool = app._captcha_cooldown_until(sess)
            assert cool is not None
            assert (cool - recent).total_seconds() == 6 * 3600
            # A clean ok run should clear the cooldown.
            sess.query(models.ScrapeRun).delete()
            sess.add(models.ScrapeRun(
                trigger="hourly", started_at=recent, finished_at=recent,
                status="ok", n_found=10, notes="all good"))
            sess.commit()
            assert app._captcha_cooldown_until(sess) is None
            # Old CAPTCHA (>backoff window) also clears.
            old = datetime.datetime.utcnow() - datetime.timedelta(hours=12)
            sess.query(models.ScrapeRun).delete()
            sess.add(models.ScrapeRun(
                trigger="hourly", started_at=old, finished_at=old,
                status="partial", n_found=0, notes="CAPTCHA at 0/40"))
            sess.commit()
            assert app._captcha_cooldown_until(sess) is None
        finally:
            sess.close()


def test_upsert_preserves_watchlist_category():
    """Discovery scrapes that find a watchlist ASIN in normal results must not
    overwrite its category with 'power_bank' / 'power_station'."""
    with _isolated_db():
        sess = models.SessionLocal()
        try:
            now = datetime.datetime.utcnow()
            sess.add(models.Product(asin="B0WL", title="Power Bank 20000mAh",
                                    category="watchlist", first_seen=now, last_seen=now))
            sess.commit()
            # Simulate a discovery upsert that assigns power_bank category.
            app._upsert(sess, {"asin": "B0WL", "title": "Power Bank 20000mAh",
                                "price": 25.0, "in_stock": True,
                                "category": "power_bank"})
            sess.commit()
            assert sess.get(models.Product, "B0WL").category == "watchlist"
        finally:
            sess.close()


def test_reconcile_catalog_skips_watchlist():
    """Watchlist products must never be auto-deleted even if their title looks
    like an accessory (user added it deliberately)."""
    with _isolated_db():
        sess = models.SessionLocal()
        try:
            now = datetime.datetime.utcnow()
            sess.add(models.Product(
                asin="B0CASE", category="watchlist", last_seen=now, first_seen=now,
                title="Hard Travel Case for Anker Power Bank 40000mAh"))
            sess.commit()
            app._reconcile_catalog(sess, healthy=True)
            assert sess.get(models.Product, "B0CASE") is not None
        finally:
            sess.close()


def test_captcha_cooldown_not_cleared_by_subsequent_non_captcha_failure():
    """A non-CAPTCHA failure after a CAPTCHA run must not clear the cooldown."""
    with _isolated_db(BBA_CAPTCHA_BACKOFF_HOURS="6"):
        sess = models.SessionLocal()
        try:
            captcha_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=30)
            fail_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
            # CAPTCHA run first, then a later plain failure with no CAPTCHA note.
            sess.add(models.ScrapeRun(
                trigger="hourly", started_at=captcha_time, finished_at=captcha_time,
                status="partial", n_found=0, notes="CAPTCHA at 0/40"))
            sess.add(models.ScrapeRun(
                trigger="hourly", started_at=fail_time, finished_at=fail_time,
                status="failed", n_found=0, notes="ConnectionError: timeout"))
            sess.commit()
            cool = app._captcha_cooldown_until(sess)
            assert cool is not None, "CAPTCHA cooldown should still be active"
        finally:
            sess.close()
