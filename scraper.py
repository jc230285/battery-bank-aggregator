"""Playwright scraper for amazon.co.uk battery-bank listings.

Two refresh paths:
- refresh_asins(asins): Playwright detail-page visits (heavyweight, needed when
  pages require JS).
- refresh_asins_http(asins): urllib + BeautifulSoup (lightweight, no browser
  fingerprint). Default for the hourly cycle — Amazon serves a full HTML
  product page to a plain curl-style request without CAPTCHA, while the same
  IP via headless Chromium gets challenged. This is why the hourly works
  reliably and the discovery search-pagination still needs Playwright.
"""
import datetime
import gzip
import logging
import os
import random
import time
import zlib
from collections import deque
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None
try:
    from playwright_stealth import stealth_sync
except ImportError:  # stealth is optional; fall back to plain playwright
    stealth_sync = None

import cache
import config
import parse

STORAGE_STATE_PATH = os.path.join(config.STATE_DIR, "amazon-storage.json")


def _page_url(base, n):
    u = urlparse(base)
    q = dict(parse_qsl(u.query))
    q["page"] = str(n)
    return urlunparse(u._replace(query=urlencode(q)))

log = logging.getLogger("scraper")


class BlockedError(RuntimeError):
    """Raised when Amazon serves a CAPTCHA / robot check."""


def _sleep():
    time.sleep(random.uniform(config.MIN_DELAY_S, config.MAX_DELAY_S))


def _is_blocked(page):
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if "robot check" in title or "sorry" in title:
        return True
    if page.locator("form[action*='validateCaptcha']").count() > 0:
        return True
    if page.locator("input#captchacharacters").count() > 0:
        return True
    return False


def _text(page, selector):
    try:
        loc = page.locator(selector).first
        if loc.count() > 0:
            return (loc.inner_text(timeout=2000) or "").strip()
    except Exception:
        pass
    return ""


def _attr(page, selector, attr):
    try:
        loc = page.locator(selector).first
        if loc.count() > 0:
            return (loc.get_attribute(attr) or "").strip()
    except Exception:
        pass
    return ""


def _collect_search_cards(page):
    """Return all cards on the current results page as dicts."""
    cards = []
    results = page.locator("div[data-component-type='s-search-result']")
    count = results.count()
    for i in range(count):
        card = results.nth(i)
        asin = (card.get_attribute("data-asin") or "").strip()
        if not asin:
            continue

        def csel(sel):
            try:
                loc = card.locator(sel).first
                return (loc.inner_text(timeout=1500) or "").strip() if loc.count() else ""
            except Exception:
                return ""

        title = csel("h2 span") or csel("h2")
        price_whole = csel("span.a-price span.a-offscreen")
        rating_txt = csel("span.a-icon-alt")
        reviews_txt = ""
        try:
            rl = card.locator("span.a-size-base.s-underline-text, span[aria-label$='ratings']").first
            if rl.count():
                reviews_txt = (rl.inner_text(timeout=1500) or "").strip()
        except Exception:
            pass
        image = ""
        try:
            img = card.locator("img.s-image").first
            if img.count():
                image = img.get_attribute("src") or ""
        except Exception:
            pass

        cards.append({
            "asin": asin,
            "title": title,
            "price": parse.parse_price(price_whole),
            "rating": parse.parse_rating(rating_txt),
            "review_count": parse.parse_review_count(reviews_txt),
            "image_url": image,
        })
    return cards


def _relevant(title, category, mah=None, wh=None):
    """Category-aware relevance: power stations vs power banks (vs accessories)."""
    if category == "power_station":
        return parse.is_power_station(title) or (wh is not None)
    return parse.is_battery_bank(title, mah)


def _collect_relevant_cards(page, search_url, category, on_progress=None, already=None):
    """Paginate one search, keeping cards relevant to `category` (tagged with it) and
    skipping ASINs already collected by another search.

    TOP_N == 0 means unlimited (until a CAPTCHA, an empty page, or MAX_PAGES).
    Returns (cards, blocked, pages_scanned). Page 1 must already be loaded.
    """
    already = already or set()
    kept = {}
    blocked = False
    pages = 0
    for page_num in range(1, config.MAX_PAGES + 1):
        if on_progress:
            on_progress(f"collecting {category} page {page_num}", len(kept), config.TOP_N)
        if page_num > 1:
            try:
                page.goto(_page_url(search_url, page_num),
                          wait_until="domcontentloaded", timeout=45000)
                if _is_blocked(page):
                    raise BlockedError(f"blocked on {category} page {page_num}")
                page.wait_for_selector(
                    "div[data-component-type='s-search-result']", timeout=15000)
            except BlockedError:
                blocked = True
                log.warning("CAPTCHA while paginating %s at page %d; keeping %d",
                            category, page_num, len(kept))
                break
            except PWTimeout:
                break
        pages = page_num
        page_cards = _collect_search_cards(page)
        new_on_page = 0
        for c in page_cards:
            if c["asin"] in kept or c["asin"] in already:
                continue
            if c["title"] and not _relevant(c["title"], category):
                continue
            c["category"] = category
            c["listing_position"] = len(kept) + 1
            kept[c["asin"]] = c
            new_on_page += 1
            if config.TOP_N and len(kept) >= config.TOP_N:
                return list(kept.values()), blocked, pages
        log.info("%s page %d: %d cards, %d new (total %d)",
                 category, page_num, len(page_cards), new_on_page, len(kept))
        if not page_cards:
            break
        _sleep()
    return list(kept.values()), blocked, pages


def _brand_searches(today=None):
    """One brand-anchored search per category per cycle, rotating daily by date so
    every brand gets covered over time without raising per-run request volume."""
    today = today or datetime.date.today()
    seed = today.toordinal()
    out = []
    for category, brands in (("power_bank", config.BRAND_BANKS),
                             ("power_station", config.BRAND_STATIONS)):
        if not brands:
            continue
        brand = brands[seed % len(brands)]
        q = brand.replace(" ", "+").lower()
        suffix = "+power+bank" if category == "power_bank" else "+lifepo4+power+station"
        out.append({"category": category, "brand": brand,
                    "url": f"https://www.amazon.co.uk/s?k={q}{suffix}&s=review-rank"})
    return out


def _scrape_detail(page, asin):
    """Visit a product page and extract specs. Returns a dict (may have Nones).

    Detail HTML is cached on disk (long TTL) so re-runs after parser changes can
    replay against cached HTML, and CAPTCHA-interrupted runs that re-detail the
    same ASIN don't pay the network cost twice.
    """
    url = f"https://www.amazon.co.uk/dp/{asin}?th=1"
    cached = cache.get(url)
    if cached:
        # Replay against cached rendered HTML — no network, no anti-bot exposure.
        page.set_content(cached, wait_until="domcontentloaded", timeout=15000)
    else:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if _is_blocked(page):
            raise BlockedError(f"blocked on product {asin}")
        # Amazon lazy-loads the lower "Product information" table (weight, Date First
        # Available) — scroll so it renders before we read the detail text.
        try:
            for frac in (0.5, 1.0):
                page.evaluate("f => window.scrollTo(0, document.body.scrollHeight * f)", frac)
                page.wait_for_timeout(350)
        except Exception:
            pass
        try:
            cache.put(url, page.content())
        except Exception as e:  # noqa: BLE001 - cache write must never abort a scrape
            log.warning("cache put failed for %s: %s", asin, e)

    brand = parse.clean_brand(_text(page, "#bylineInfo"))
    # Many low-reputation listings show a seller-code byline (rejected above);
    # the title almost always still leads with the actual brand name.
    if not brand:
        title_for_brand = _text(page, "#productTitle")
        known = config.TRUSTED_BRANDS + config.BRAND_BANKS + config.BRAND_STATIONS
        brand = parse.brand_from_title(title_for_brand, known)

    bullets = _text(page, "#feature-bullets")
    details = " ".join([
        _text(page, "#productDetails_techSpec_section_1"),
        _text(page, "#productDetails_techSpec_section_2"),
        _text(page, "#productDetails_detailBullets_sections1"),
        _text(page, "#productDetails_db_sections"),
        _text(page, "#technicalSpecifications_section_1"),
        _text(page, "#detailBullets_feature_div"),
        _text(page, "#detailBulletsWrapper_feature_div"),
        _text(page, "#productDetailsTable"),
        _text(page, "table.prodDetTable"),
        _text(page, "#prodDetails"),
    ])
    title = _text(page, "#productTitle")
    # Buy-box first, then variant-aware selectors, then the page-wide fallback —
    # multi-variant pages (e.g. EcoFlow Delta Pro 3 with several SKUs) otherwise
    # match a teaser/accessory price instead of the selected variant.
    price_txt = (
        _text(page, "#corePriceDisplay_desktop_feature_div .priceToPay span.a-offscreen")
        or _text(page, "#corePrice_feature_div .priceToPay span.a-offscreen")
        or _text(page, "#corePrice_feature_div span.a-offscreen")
        or _text(page, "#priceblock_ourprice")
        or _text(page, "#price_inside_buybox")
        or _text(page, "#apex_desktop .priceToPay span.a-offscreen")
        or _text(page, "span.olpWrapper")  # "1 option from £X" pattern on variant pages
        or _text(page, "span.a-price span.a-offscreen")
    )
    availability = _text(page, "#availability").lower()
    rating_txt = (_text(page, "#averageCustomerReviews span.a-icon-alt")
                  or _attr(page, "#acrPopover", "title")
                  or _text(page, "span[data-hook='rating-out-of-text']"))
    reviews_txt = _text(page, "#acrCustomerReviewText")

    blob = " \n ".join([title, bullets, details])

    pd_w, max_w = parse.extract_watts(blob)
    usb_a, usb_c = parse.extract_ports(blob)
    feats = parse.detect_features(blob)
    spec_blob = details + " " + title  # specs/title most reliable for chemistry & power-station fields

    snippets = []
    try:
        rev = page.locator("[data-hook='review-body']")
        for i in range(min(rev.count(), 8)):
            t = (rev.nth(i).inner_text(timeout=1500) or "").strip()
            if t:
                snippets.append(t)
    except Exception:
        pass

    in_stock = True
    if availability and ("unavailable" in availability or "out of stock" in availability):
        in_stock = False

    return {
        "asin": asin,
        "url": f"https://www.amazon.co.uk/dp/{asin}",
        "brand": brand or None,
        "title": title or None,
        "price": parse.parse_price(price_txt),
        "in_stock": in_stock,
        "rating": parse.parse_rating(rating_txt),
        "review_count": parse.parse_review_count(reviews_txt),
        "claimed_mah": parse.extract_mah(title) or parse.extract_mah(blob),
        # Title is the most authoritative source for the headline spec; only fall
        # back to bullets/details when the title carries no Wh number.
        "capacity_wh": parse.extract_wh(title) or parse.extract_wh(blob),
        "chemistry": parse.extract_chemistry(spec_blob),
        "weight_g": parse.extract_weight_g(details) or parse.extract_weight_g(blob),
        "date_first_available": parse.extract_date_first_available(details)
        or parse.extract_date_first_available(blob),
        "usb_a": usb_a,
        "usb_c": usb_c,
        "max_w": max_w,
        "pd_w": pd_w,
        "wireless": feats["wireless"],
        "display": feats["display"],
        "passthrough": feats["passthrough"],
        "solar": feats["solar"],
        "ac_output_w": parse.extract_ac_output_w(blob),
        "ac_sockets": parse.extract_ac_sockets(blob),
        "solar_input_w": parse.extract_solar_input_w(blob),
        "cycle_life": parse.extract_cycle_life(blob),
        "expandable": feats["expandable"],
        "ups": feats["ups"],
        "raw_specs": {"bullets": bullets[:4000], "details": details[:4000]},
        "review_snippets": snippets,
    }


def order_detail_cards(cards, known_asins=None):
    """Order cards for detail scraping: interleave categories round-robin, with
    unseen products first within each category. This way a CAPTCHA mid-run leaves
    every category populated instead of starving the ones scraped last."""
    groups = {}
    for c in cards:
        groups.setdefault(c.get("category", "power_bank"), []).append(c)
    if known_asins:
        for g in groups.values():
            g.sort(key=lambda c: c["asin"] in known_asins)  # stable: new (False) first
    queues = [deque(g) for g in groups.values()]
    out = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.popleft())
    return out


def _card_only_item(c):
    """A price/stock refresh built from the search card alone (no detail page).

    Omits title and specs so an upsert preserves the richer detail-scraped data.
    """
    return {
        "asin": c["asin"],
        "url": f"https://www.amazon.co.uk/dp/{c['asin']}",
        "price": c.get("price"),
        "rating": c.get("rating"),
        "review_count": c.get("review_count"),
        "image_url": c.get("image_url"),
        "listing_position": c.get("listing_position"),
    }


def _open_browser_context(pw):
    """Browser+context+page with persistent storage_state and stealth applied.
    Shared by the full discovery scrape and the lightweight hourly refresh."""
    browser = pw.chromium.launch(headless=config.HEADLESS)
    ctx_kwargs = dict(
        locale=config.LOCALE, user_agent=config.USER_AGENT,
        viewport={"width": 1366, "height": 900},
    )
    if os.path.exists(STORAGE_STATE_PATH):
        ctx_kwargs["storage_state"] = STORAGE_STATE_PATH
    ctx = browser.new_context(**ctx_kwargs)
    page = ctx.new_page()
    if stealth_sync is not None:
        try:
            stealth_sync(page)
        except Exception as e:  # noqa: BLE001
            log.warning("stealth patches failed: %s", e)
    return browser, ctx, page


def _save_storage_state(ctx):
    try:
        os.makedirs(os.path.dirname(STORAGE_STATE_PATH), exist_ok=True)
        ctx.storage_state(path=STORAGE_STATE_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("storage_state save failed: %s", e)


def _http_get(url, timeout=20):
    """Plain HTTPS GET that mimics a real browser request enough for Amazon to
    serve a full product page. Handles gzip/deflate so the response is text."""
    req = Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    })
    with urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


def _bs_text(soup, *selectors):
    """First selector that yields non-empty text wins."""
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            t = node.get_text(" ", strip=True)
            if t:
                return t
    return ""


def _parse_detail_html(html, asin):
    """Same shape as _scrape_detail's return value, parsed from raw HTML rather
    than a live Playwright page. The selectors mirror the Playwright ones so
    the two paths populate the catalog identically."""
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is required for HTTP refresh")
    soup = BeautifulSoup(html, "lxml")
    # Captcha / interstitial detection
    if soup.select_one("form[action*='validateCaptcha'], input#captchacharacters"):
        raise BlockedError(f"blocked on product {asin}")

    title = _bs_text(soup, "#productTitle")
    brand = parse.clean_brand(_bs_text(soup, "#bylineInfo"))
    if not brand:
        known = config.TRUSTED_BRANDS + config.BRAND_BANKS + config.BRAND_STATIONS
        brand = parse.brand_from_title(title, known)
    bullets = _bs_text(soup, "#feature-bullets")
    details = " ".join(filter(None, [
        _bs_text(soup, "#productDetails_techSpec_section_1"),
        _bs_text(soup, "#productDetails_techSpec_section_2"),
        _bs_text(soup, "#productDetails_detailBullets_sections1"),
        _bs_text(soup, "#productDetails_db_sections"),
        _bs_text(soup, "#technicalSpecifications_section_1"),
        _bs_text(soup, "#detailBullets_feature_div"),
        _bs_text(soup, "#detailBulletsWrapper_feature_div"),
        _bs_text(soup, "#productDetailsTable"),
        _bs_text(soup, "table.prodDetTable"),
        _bs_text(soup, "#prodDetails"),
    ]))
    price_txt = _bs_text(
        soup,
        "#corePriceDisplay_desktop_feature_div .priceToPay span.a-offscreen",
        "#corePrice_feature_div .priceToPay span.a-offscreen",
        "#corePrice_feature_div span.a-offscreen",
        "#priceblock_ourprice",
        "#price_inside_buybox",
        "#apex_desktop .priceToPay span.a-offscreen",
        "span.olpWrapper",
        "span.a-price span.a-offscreen",
    )
    availability = _bs_text(soup, "#availability").lower()
    rating_txt = _bs_text(
        soup,
        "#averageCustomerReviews span.a-icon-alt",
        "span[data-hook='rating-out-of-text']",
    )
    if not rating_txt:
        acr = soup.select_one("#acrPopover")
        if acr:
            rating_txt = acr.get("title") or ""
    reviews_txt = _bs_text(soup, "#acrCustomerReviewText")
    snippets = []
    for node in soup.select("[data-hook='review-body']")[:8]:
        t = node.get_text(" ", strip=True)
        if t:
            snippets.append(t)
    in_stock = not (availability and ("unavailable" in availability
                                      or "out of stock" in availability))

    blob = " \n ".join([title, bullets, details])
    pd_w, max_w = parse.extract_watts(blob)
    usb_a, usb_c = parse.extract_ports(blob)
    feats = parse.detect_features(blob)
    spec_blob = details + " " + title

    return {
        "asin": asin,
        "url": f"https://www.amazon.co.uk/dp/{asin}",
        "brand": brand or None,
        "title": title or None,
        "price": parse.parse_price(price_txt),
        "in_stock": in_stock,
        "rating": parse.parse_rating(rating_txt),
        "review_count": parse.parse_review_count(reviews_txt),
        "claimed_mah": parse.extract_mah(title) or parse.extract_mah(blob),
        "capacity_wh": parse.extract_wh(title) or parse.extract_wh(blob),
        "chemistry": parse.extract_chemistry(spec_blob),
        "weight_g": parse.extract_weight_g(details) or parse.extract_weight_g(blob),
        "date_first_available": parse.extract_date_first_available(details)
        or parse.extract_date_first_available(blob),
        "usb_a": usb_a, "usb_c": usb_c, "max_w": max_w, "pd_w": pd_w,
        "wireless": feats["wireless"], "display": feats["display"],
        "passthrough": feats["passthrough"], "solar": feats["solar"],
        "ac_output_w": parse.extract_ac_output_w(blob),
        "ac_sockets": parse.extract_ac_sockets(blob),
        "solar_input_w": parse.extract_solar_input_w(blob),
        "cycle_life": parse.extract_cycle_life(blob),
        "expandable": feats["expandable"], "ups": feats["ups"],
        "raw_specs": {"bullets": bullets[:4000], "details": details[:4000]},
        "review_snippets": snippets,
    }


def refresh_asins_http(asins, on_item=None, on_progress=None):
    """Lightweight hourly refresh via urllib+BeautifulSoup. No browser, no
    fingerprint. Returns (items, status, notes), same shape as refresh_asins."""
    asins = list(dict.fromkeys(a for a in (asins or []) if a))
    if not asins:
        return [], "ok", "no asins to refresh"
    items = []
    notes = ""
    status = "ok"
    t0 = time.time()

    def progress(phase, done, total):
        if on_progress:
            on_progress(phase, done, total)

    for i, asin in enumerate(asins):
        progress("refreshing", i, len(asins))
        url = f"https://www.amazon.co.uk/dp/{asin}?th=1"
        try:
            html = _http_get(url)
        except HTTPError as e:
            if e.code in (404, 410):
                notes += f"{asin} delisted (HTTP {e.code}); "
            else:
                notes += f"{asin} HTTP {e.code}; "
            continue
        except (URLError, TimeoutError) as e:
            notes += f"{asin} fetch failed; "
            continue
        try:
            detail = _parse_detail_html(html, asin)
        except BlockedError:
            status = "partial"
            notes += f"CAPTCHA at {i}/{len(asins)}; "
            break
        except Exception as e:  # noqa: BLE001
            log.warning("parse failed for %s: %s", asin, e)
            notes += f"parse {asin} failed; "
            continue
        # Cache the raw HTML so subsequent parser changes can replay offline.
        try:
            cache.put(url, html)
        except Exception as e:  # noqa: BLE001
            log.warning("cache put failed for %s: %s", asin, e)
        items.append(detail)
        if on_item:
            try:
                on_item(detail)
            except Exception as e:  # noqa: BLE001
                log.warning("on_item failed for %s: %s", asin, e)
        time.sleep(random.uniform(config.MIN_DELAY_S, config.MAX_DELAY_S))

    if not items and status == "ok":
        status = "failed"
        notes += "no items refreshed; "
    dur = time.time() - t0
    stats = f"[http-refresh n={len(asins)} got={len(items)} dur={dur:.0f}s]"
    log.info("hourly http refresh: %s status=%s", stats, status)
    notes = (notes + " " + stats).strip()
    return items, status, notes


def refresh_asins(asins, on_item=None, on_progress=None):
    """Lightweight hourly refresh: direct detail-page visits for an explicit
    list of ASINs. No search pagination. Returns (items, status, notes).

    Used both for the hourly oldest-N refresh and for a one-shot watchlist add.
    A CAPTCHA mid-run sets status=partial and breaks; whatever was collected so
    far is kept."""
    asins = list(dict.fromkeys(a for a in (asins or []) if a))  # dedupe, preserve order
    if not asins:
        return [], "ok", "no asins to refresh"
    items = []
    notes = ""
    status = "ok"
    t0 = time.time()
    cache.reset_stats()

    def progress(phase, done, total):
        if on_progress:
            on_progress(phase, done, total)

    progress("launching browser", 0, len(asins))
    with sync_playwright() as pw:
        browser, ctx, page = _open_browser_context(pw)
        try:
            for i, asin in enumerate(asins):
                progress("refreshing", i, len(asins))
                _sleep()
                try:
                    detail = _scrape_detail(page, asin)
                except BlockedError:
                    status = "partial"
                    notes += f"CAPTCHA at {i}/{len(asins)}; "
                    break
                except Exception as e:  # noqa: BLE001 - keep going on per-product failures
                    log.warning("refresh detail failed for %s: %s", asin, e)
                    notes += f"detail {asin} failed; "
                    continue
                items.append(detail)
                if on_item:
                    try:
                        on_item(detail)
                    except Exception as e:  # noqa: BLE001 - persistence must not abort
                        log.warning("on_item failed for %s: %s", asin, e)
        finally:
            _save_storage_state(ctx)
            ctx.close()
            browser.close()

    if not items and status == "ok":
        status = "failed"
        notes += "no items refreshed; "
    dur = time.time() - t0
    cs = cache.stats
    stats = (f"[refresh n={len(asins)} got={len(items)} dur={dur:.0f}s "
             f"cache hits={cs['hits']} misses={cs['misses']}]")
    log.info("hourly refresh: %s status=%s", stats, status)
    notes = (notes + " " + stats).strip()
    return items, status, notes


def scrape(on_item=None, on_progress=None, known_asins=None, detailed_asins=None,
           full=False, category_counts=None, watchlist_asins=None):
    """Run a scrape. Calls on_item(item) per product and on_progress(phase, done, total).

    - The least-populated category is collected first, and detail scraping is
      interleaved across categories (unseen products first within each), so a
      CAPTCHA mid-run never starves an empty/under-covered category.
    - Unless full=True, products already in detailed_asins (have full specs) skip
      the detail page and are refreshed from the search card only — this keeps
      routine runs fast and avoids hammering Amazon. Specs are static per ASIN.

    Returns (items, status, notes). status in ok|partial|failed.
    """
    detailed_asins = detailed_asins or set()
    items = []
    notes = ""
    status = "ok"
    t0 = time.time()
    pages = 0
    page_blocked = False

    def progress(phase, done, total):
        if on_progress:
            on_progress(phase, done, total)

    cache.reset_stats()
    progress("launching browser", 0, config.TOP_N)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.HEADLESS)
        ctx_kwargs = dict(
            locale=config.LOCALE, user_agent=config.USER_AGENT,
            viewport={"width": 1366, "height": 900},
        )
        # Persist cookies/localStorage between runs so Amazon sees a returning
        # visitor rather than a fresh fingerprint each cycle (CAPTCHA-mitigation).
        if os.path.exists(STORAGE_STATE_PATH):
            ctx_kwargs["storage_state"] = STORAGE_STATE_PATH
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        # Apply playwright-stealth patches (navigator.webdriver, plugins, languages,
        # chrome runtime) — reduces but doesn't eliminate Amazon's bot detection.
        if stealth_sync is not None:
            try:
                stealth_sync(page)
            except Exception as e:  # noqa: BLE001 - stealth is best-effort
                log.warning("stealth patches failed: %s", e)
        try:
            # Collect cards from every configured search, tagging each with its
            # category and deduping ASINs across searches.
            cards = []
            seen = set()
            # Collect the least-populated category first (an empty new category
            # shouldn't wait behind a fully-populated one). Brand-anchored searches
            # rotate daily and ride alongside the generic ones — bigger long-tail
            # coverage without spiking per-run page count.
            counts = category_counts or {}
            all_searches = list(config.SEARCHES) + _brand_searches()
            searches = sorted(all_searches, key=lambda sr: counts.get(sr["category"], 0))
            for search in searches:
                url, category = search["url"], search["category"]
                progress(f"opening {category} search", len(cards), config.TOP_N)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    if _is_blocked(page):
                        raise BlockedError(f"blocked on {category} search")
                    page.wait_for_selector(
                        "div[data-component-type='s-search-result']", timeout=15000)
                except BlockedError as e:
                    page_blocked = True
                    notes += f"{e}; "
                    break
                except PWTimeout:
                    notes += f"no results for {category}; "
                    continue
                cards_s, blocked_s, pages_s = _collect_relevant_cards(
                    page, url, category, on_progress=on_progress, already=seen)
                cards.extend(cards_s)
                seen.update(c["asin"] for c in cards_s)
                pages += pages_s
                if blocked_s:
                    page_blocked = True
                    notes += f"CAPTCHA paginating {category} (kept {len(cards_s)}); "
                    break

            log.info("collected %d relevant cards across %d searches",
                     len(cards), len(config.SEARCHES))
            # Add user-curated watchlist ASINs as synthetic cards so the same
            # detail-scrape loop refreshes them. They have no card data and
            # bypass the relevance filter further down.
            if watchlist_asins:
                seen_asins = {c["asin"] for c in cards}
                for a in watchlist_asins:
                    if a not in seen_asins:
                        cards.append({"asin": a, "category": "watchlist",
                                      "title": "", "price": None, "rating": None,
                                      "review_count": None, "image_url": "",
                                      "listing_position": None})
            if not cards and not page_blocked:
                raise RuntimeError("no search results found")
            cards = order_detail_cards(cards, known_asins)
            log.info("scrape order: interleaved %d cards across categories", len(cards))
            if page_blocked:
                status = "partial"
                notes += f"CAPTCHA while paginating (got {len(cards)} cards over {pages} pages); "

            by_asin = {c["asin"]: c for c in cards}
            total = len(cards)
            for c in cards:
                progress("scraping products", len(items), total)
                # Known, fully-detailed products only need a price refresh from the
                # card (unless a full re-scrape was requested).
                if not full and c["asin"] in detailed_asins:
                    item = _card_only_item(c)
                    items.append(item)
                    if on_item:
                        try:
                            on_item(item)
                        except Exception as e:  # noqa: BLE001
                            log.warning("on_item (card-only) failed for %s: %s", c["asin"], e)
                    continue

                _sleep()
                try:
                    detail = _scrape_detail(page, c["asin"])
                except BlockedError:
                    status = "partial"
                    notes += f"CAPTCHA during detail scrape at {len(items)}/{total}; "
                    break
                except Exception as e:  # noqa: BLE001 - per-product isolation
                    log.warning("detail failed for %s: %s", c["asin"], e)
                    notes += f"detail {c['asin']} failed; "
                    continue
                # search-card fields fill gaps in detail
                for k in ("rating", "review_count", "image_url", "listing_position"):
                    if detail.get(k) in (None, "") and by_asin[c["asin"]].get(k) is not None:
                        detail[k] = by_asin[c["asin"]][k]
                    elif k not in detail:
                        detail[k] = by_asin[c["asin"]].get(k)
                if not detail.get("price"):
                    detail["price"] = c.get("price")
                if not detail.get("title"):
                    detail["title"] = c.get("title")
                detail["category"] = c.get("category")
                # final relevance gate (category-aware) using the fuller detail data.
                # Watchlist items are user-chosen — never gate them on relevance.
                if (c.get("category") != "watchlist" and
                        not _relevant(detail.get("title"), c.get("category"),
                                      detail.get("claimed_mah"), detail.get("capacity_wh"))):
                    log.info("skipping irrelevant %s: %r", c["asin"], (detail.get("title") or "")[:60])
                    notes += f"skipped irrelevant {c['asin']}; "
                    continue
                items.append(detail)
                if on_item:
                    try:
                        on_item(detail)
                    except Exception as e:  # noqa: BLE001 - persistence must not abort scrape
                        log.warning("on_item failed for %s: %s", c["asin"], e)
        except BlockedError as e:
            status = "failed" if not items else "partial"
            notes += f"{e}; "
            log.warning("scrape blocked: %s", e)
        finally:
            try:
                os.makedirs(os.path.dirname(STORAGE_STATE_PATH), exist_ok=True)
                ctx.storage_state(path=STORAGE_STATE_PATH)
            except Exception as e:  # noqa: BLE001 - persistence best-effort
                log.warning("storage_state save failed: %s", e)
            ctx.close()
            browser.close()

    if not items and status == "ok":
        status = "failed"
        notes += "no items collected; "
    # Parse-yield guard: if most items have no price, the selectors likely broke
    # and we're silently storing empty rows — surface that instead of "ok".
    priced = sum(1 for it in items if it.get("price"))
    if len(items) >= 10 and priced / len(items) < 0.5:
        if status == "ok":
            status = "partial"
        notes += f"low parse yield ({priced}/{len(items)} priced); "
        log.warning("low parse yield: only %d/%d items have a price", priced, len(items))
    dur = time.time() - t0
    cs = cache.stats
    stats = (f"[pages={pages} scraped={len(items)} dur={dur:.0f}s "
             f"cache hits={cs['hits']} misses={cs['misses']} writes={cs['writes']}]")
    log.info("scrape limits: %s status=%s", stats, status)
    notes = (notes + " " + stats).strip()
    return items, status, notes.strip()
