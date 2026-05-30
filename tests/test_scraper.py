"""Unit tests for pure scraper helpers (no browser needed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper


def test_page_url_sets_page_and_keeps_params():
    base = "https://www.amazon.co.uk/s?k=battery+bank&s=review-rank"
    u2 = scraper._page_url(base, 2)
    assert "page=2" in u2
    assert "k=battery+bank" in u2
    assert "s=review-rank" in u2
    # changing page replaces, doesn't duplicate
    u3 = scraper._page_url(u2, 3)
    assert "page=3" in u3
    assert u3.count("page=") == 1


def test_card_only_item_preserves_specs():
    card = {"asin": "B0TEST1234", "title": "trunc title", "price": 19.99,
            "rating": 4.5, "review_count": 1200, "image_url": "http://img",
            "listing_position": 3}
    item = scraper._card_only_item(card)
    # refreshes price/rating/reviews/image/position
    assert item["price"] == 19.99 and item["rating"] == 4.5
    assert item["review_count"] == 1200 and item["listing_position"] == 3
    assert item["url"].endswith("/dp/B0TEST1234")
    # must NOT carry title or specs, so an upsert preserves the detailed values
    for spec in ("title", "claimed_mah", "weight_g", "usb_a", "pd_w", "wireless"):
        assert spec not in item


def test_order_detail_cards_interleaves_and_new_first():
    cards = [
        {"asin": "pb1", "category": "power_bank"},
        {"asin": "pb2", "category": "power_bank"},
        {"asin": "pb3", "category": "power_bank"},
        {"asin": "ps1", "category": "power_station"},
    ]
    out = [c["asin"] for c in scraper.order_detail_cards(cards, known_asins={"pb1"})]
    # the lone power_station is interleaved early, not starved at the end
    assert out.index("ps1") <= 2
    # within power_bank, unseen (pb2/pb3) come before the known pb1
    assert out.index("pb2") < out.index("pb1") and out.index("pb3") < out.index("pb1")
    assert sorted(out) == ["pb1", "pb2", "pb3", "ps1"]


def test_brand_searches_url_format():
    import datetime as dt
    today = dt.date(2026, 5, 29)
    searches = scraper._brand_searches(today)
    assert len(searches) == 2
    bank = next(s for s in searches if s["category"] == "power_bank")
    station = next(s for s in searches if s["category"] == "power_station")
    assert "+power+bank" in bank["url"]
    assert "+portable+power+station" in station["url"]
    assert "amazon.co.uk" in bank["url"]
    assert "s=review-rank" in station["url"]
    assert bank.get("brand")  # a brand is always picked


def test_parse_detail_html_detects_title_captcha():
    # Amazon's "Robot Check" page has a recognisable <title> — must raise BlockedError.
    html = "<html><head><title>Robot Check</title></head><body><p>sorry</p></body></html>"
    try:
        scraper._parse_detail_html(html, "B0TEST")
        assert False, "expected BlockedError"
    except scraper.BlockedError:
        pass


def test_new_first_ordering_key():
    # The scraper sorts cards so unseen ASINs come before known ones.
    known = {"b", "d"}
    cards = [{"asin": a} for a in ["a", "b", "c", "d"]]
    cards.sort(key=lambda c: c["asin"] in known)
    assert [c["asin"] for c in cards] == ["a", "c", "b", "d"]


def test_parse_detail_html_extracts_core_fields():
    """_parse_detail_html should populate price, title, mAh, weight, and chemistry
    from a minimal but realistic Amazon product page structure."""
    html = """
    <html><head><title>Anker Power Bank</title></head><body>
      <span id="productTitle">Anker 737 Power Bank 26800mAh</span>
      <span id="bylineInfo">Brand: Anker</span>
      <div id="corePriceDisplay_desktop_feature_div">
        <span class="priceToPay"><span class="a-offscreen">£79.99</span></span>
      </div>
      <div id="availability"><span>In Stock</span></div>
      <div id="acrPopover" title="4.6 out of 5 stars"></div>
      <span id="acrCustomerReviewText">12,345 ratings</span>
      <div id="feature-bullets">
        <span>26800mAh Li-ion power bank</span>
        <span>Weight: 635 grams</span>
        <span>65W USB-C PD fast charging</span>
      </div>
      <div id="productDetails_techSpec_section_1">
        <tr><td>Item Weight</td><td>635 g</td></tr>
      </div>
    </body></html>
    """
    result = scraper._parse_detail_html(html, "B0ANKER01")
    assert result["title"] == "Anker 737 Power Bank 26800mAh"
    assert result["price"] == 79.99
    assert result["claimed_mah"] == 26800
    assert result["weight_g"] == 635
    assert result["chemistry"] == "li-ion"
    assert result["pd_w"] == 65
    assert result["review_count"] == 12345
    assert result["in_stock"] is True
    assert result["rating"] == 4.6
    assert result["asin"] == "B0ANKER01"
