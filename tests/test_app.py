"""Tests for app-level serialization the UI depends on (no server/DB needed)."""
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import models


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
