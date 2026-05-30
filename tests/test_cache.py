"""Unit tests for the HTML cache and brand-rotation helper (no browser)."""
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache
import config
import scraper


def _isolate_cache(tmp):
    config.CACHE_DIR = tmp
    cache.reset_stats()


def test_cache_miss_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_cache(tmp)
        assert cache.get("https://example.com/never-cached") is None
        assert cache.stats["misses"] == 1


def test_cache_put_then_get_returns_html():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_cache(tmp)
        url = "https://www.amazon.co.uk/dp/B0TEST"
        cache.put(url, "<html><body>hi</body></html>")
        assert cache.stats["writes"] == 1
        got = cache.get(url)
        assert got == "<html><body>hi</body></html>"
        assert cache.stats["hits"] == 1


def test_cache_get_respects_ttl():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_cache(tmp)
        url = "https://www.amazon.co.uk/dp/B0EXPIRED"
        cache.put(url, "<html>old</html>")
        # max_age_s=0 forces every entry to look stale
        assert cache.get(url, max_age_s=0) is None
        assert cache.stats["misses"] == 1


def test_brand_searches_one_per_category_and_rotates():
    today = datetime.date(2026, 5, 30)
    out = scraper._brand_searches(today)
    cats = [s["category"] for s in out]
    assert cats == ["power_bank", "power_station"]
    for s in out:
        assert s["url"].startswith("https://www.amazon.co.uk/s?k=")
        assert "brand" in s and s["brand"]
    # Same date -> same pick; next day -> different pick (for >1-brand lists)
    again = scraper._brand_searches(today)
    assert [s["brand"] for s in again] == [s["brand"] for s in out]
    tomorrow = scraper._brand_searches(today + datetime.timedelta(days=1))
    assert tomorrow[0]["brand"] != out[0]["brand"]


def test_brand_searches_full_rotation_covers_every_brand():
    seen_banks = set()
    seen_stations = set()
    start = datetime.date(2026, 1, 1)
    for i in range(max(len(config.BRAND_BANKS), len(config.BRAND_STATIONS))):
        s = scraper._brand_searches(start + datetime.timedelta(days=i))
        seen_banks.add(s[0]["brand"])
        seen_stations.add(s[1]["brand"])
    assert seen_banks == set(config.BRAND_BANKS)
    assert seen_stations == set(config.BRAND_STATIONS)
