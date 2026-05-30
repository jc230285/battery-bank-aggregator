"""On-disk HTML cache so re-parses skip the network. Detail pages live here
keyed by URL; a TTL expires stale entries. Used by the scraper to avoid hitting
Amazon for products we've already fetched, and to let parsers re-run offline."""
import hashlib
import json
import os
import time

import config

stats = {"hits": 0, "misses": 0, "writes": 0}


def _path(url, suffix=".html"):
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(config.CACHE_DIR, h + suffix)


def get(url, max_age_s=None):
    """Return cached HTML for `url` if present and fresh, else None.

    `max_age_s` overrides config.CACHE_TTL_HOURS for one-off needs.
    """
    ttl = max_age_s if max_age_s is not None else config.CACHE_TTL_HOURS * 3600
    html_p, meta_p = _path(url), _path(url, ".meta")
    if not (os.path.exists(html_p) and os.path.exists(meta_p)):
        stats["misses"] += 1
        return None
    try:
        with open(meta_p, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if time.time() - float(meta.get("ts", 0)) > ttl:
            stats["misses"] += 1
            return None
        with open(html_p, "r", encoding="utf-8") as f:
            stats["hits"] += 1
            return f.read()
    except Exception:
        stats["misses"] += 1
        return None


def put(url, html):
    """Persist `html` for `url` with the current timestamp. Best-effort."""
    if not html:
        return
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    html_p, meta_p = _path(url), _path(url, ".meta")
    with open(html_p, "w", encoding="utf-8") as f:
        f.write(html)
    with open(meta_p, "w", encoding="utf-8") as f:
        json.dump({"url": url, "ts": time.time()}, f)
    stats["writes"] += 1


def reset_stats():
    stats["hits"] = 0
    stats["misses"] = 0
    stats["writes"] = 0
