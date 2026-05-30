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
