import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parse


def test_extract_mah_variants():
    assert parse.extract_mah("Power Bank 26800mAh portable") == 26800
    assert parse.extract_mah("Capacity: 20,000 mAh") == 20000
    assert parse.extract_mah("10000 mAh and 5000mAh cells") == 10000  # largest
    assert parse.extract_mah("no capacity here") is None


def test_extract_weight_units():
    assert parse.extract_weight_g("Item Weight 350 g") == 350
    assert parse.extract_weight_g("Item Weight 0.35 Kilograms") == 350
    assert round(parse.extract_weight_g("Weight: 12.3 ounces")) == 349
    assert parse.extract_weight_g("no weight") is None
    # 'kg' must not be misread as grams
    assert parse.extract_weight_g("1.2 kg pack") == 1200


def test_extract_weight_amazon_formats():
    # Amazon spec rows with invisible directionality marks around the colon
    assert parse.extract_weight_g("Item Weight ‏ : ‎ 350 Grams") == 350
    assert parse.extract_weight_g("Item Weight‎:‎1.2 Kilograms") == 1200
    # weight embedded in the dimensions row
    assert parse.extract_weight_g(
        "Product Dimensions : 10.5 x 5 x 2.5 cm; 240 g") == 240
    # label-based match should win over an unrelated leading number
    assert parse.extract_weight_g(
        "Capacity 20000 mAh. Item Weight: 430 g") == 430
    # implausible misparses ("5G" / "5V 5A" -> 5 g) must be rejected
    assert parse.extract_weight_g("Supports 5G WiFi, 5V/5A output 22.5W") is None
    assert parse.extract_weight_g("USB 3.0 ports, 10 g something") is None
    # but a real labelled weight near such noise is still found
    assert parse.extract_weight_g("5G ready. Item Weight: 365 g") == 365


def test_extract_watts_pd():
    pd, mx = parse.extract_watts("USB-C Power Delivery 65W output, also 22.5W")
    assert pd == 65
    assert mx == 65
    pd, mx = parse.extract_watts("Fast charging 20W PD and 18W QC")
    assert pd == 20
    # "65W USB-C PD" — port label between wattage and PD keyword
    pd2, _ = parse.extract_watts("65W USB-C PD fast charging")
    assert pd2 == 65
    # "100W GaN USB-C PD" — GaN + port label between wattage and PD
    pd3, _ = parse.extract_watts("100W GaN USB-C PD charger")
    assert pd3 == 100
    # "PD3.1 140W" — PD version number before wattage
    pd4, _ = parse.extract_watts("PD3.1 140W output")
    assert pd4 == 140


def test_extract_watts_decimal_and_va():
    # decimal wattage must not be truncated to "5W"
    assert parse.extract_watts("22.5W fast charging") == (None, 22.5)
    # watts inferred from voltage/current pairs (max of 15/18/18)
    pd, mx = parse.extract_watts("Output 5V/3A, 9V/2A, 12V/1.5A")
    assert mx == 18.0 and pd is None
    # AC mains input (100-240V) must be ignored; only 5V/2A output counts
    pd, mx = parse.extract_watts("Input: 100-240V 0.5A. Output: 5V 2A")
    assert mx == 10.0


def test_extract_ports():
    a, c = parse.extract_ports("2 x USB-A and 1 USB-C ports")
    assert a == 2 and c == 1
    a, c = parse.extract_ports("Single Type-C input")
    assert c == 1


def test_extract_ports_bare_usb_and_words():
    # classic "USB" port counts as USB-A
    a, c = parse.extract_ports("3 USB ports and 1 Type-C output")
    assert a == 3 and c == 1
    # "dual USB" -> 2
    a, c = parse.extract_ports("Dual USB + USB-C")
    assert a == 2 and c == 1
    # micro-USB input must NOT inflate USB-A; only the 2 USB outputs count
    a, c = parse.extract_ports("USB-C in/out, Micro USB input, 2 USB output")
    assert a == 2 and c == 1
    # a pure USB-C product should not invent a USB-A port
    a, c = parse.extract_ports("Single USB-C port only")
    assert a is None and c == 1


def test_detect_features():
    f = parse.detect_features("Qi wireless charging with LCD display and solar panel")
    assert f["wireless"] and f["display"] and f["solar"]
    assert not f["passthrough"]


def test_price_and_rating():
    assert parse.parse_price("£19.99") == 19.99
    assert parse.parse_price("Now £1,299.00") == 1299.00
    assert parse.parse_rating("4.5 out of 5 stars") == 4.5
    assert parse.parse_review_count("1,234 ratings") == 1234


def test_parse_review_count_formats():
    assert parse.parse_review_count("1,234 ratings") == 1234
    assert parse.parse_review_count("12,345 global ratings") == 12345
    assert parse.parse_review_count("1,234") == 1234          # bare number
    assert parse.parse_review_count("(2,345)") == 2345        # parenthesised
    assert parse.parse_review_count("1.2K ratings") == 1200   # K suffix
    assert parse.parse_review_count("12.3k") == 12300
    assert parse.parse_review_count("4.5 out of 5 stars") is None  # rating, not count
    assert parse.parse_review_count("") is None


def test_is_accessory():
    # definitive accessories
    assert parse.is_accessory("Hard Travel Case for Anker Power Bank")
    assert parse.is_accessory("Screen Protector 2-pack")
    # NOT definitive -> must never be deleted as an accessory
    assert not parse.is_accessory("Anker Power Bank 20000mAh with carry case")
    assert not parse.is_accessory("INIU Power Bank 10000mAh")
    assert not parse.is_accessory("")


def test_extract_date_first_available():
    assert parse.extract_date_first_available(
        "Date First Available ‏ : ‎ 12 May 2023") == "2023-05-12"
    assert parse.extract_date_first_available(
        "Date First Available: May 12, 2023") == "2023-05-12"
    assert parse.extract_date_first_available(
        "... Date first listed on Amazon 3 January 2024 ...") == "2024-01-03"
    assert parse.extract_date_first_available("no such field here") is None
    assert parse.extract_date_first_available("") is None


def test_clean_brand():
    assert parse.clean_brand("Visit the Anker Store") == "Anker"
    assert parse.clean_brand("Brand: INIU") == "INIU"
    assert parse.clean_brand("by UGREEN") == "UGREEN"
    assert parse.clean_brand("Baseus Store") == "Baseus"
    assert parse.clean_brand("ANKER") == "ANKER"
    assert parse.clean_brand("  Visit the Goal Zero Store ") == "Goal Zero"
    assert parse.clean_brand("") is None
    assert parse.clean_brand(None) is None


def test_brand_from_title():
    known = ["Anker", "Anker SOLIX", "EF ECOFLOW", "VTOMAN", "EcoFlow"]
    # Known brand wins (longest-match-first so 'EF ECOFLOW' beats 'EcoFlow').
    assert parse.brand_from_title("EF ECOFLOW DELTA Pro 3 LFP Battery", known) == "EF ECOFLOW"
    assert parse.brand_from_title("VTOMAN J1500 Power Station", known) == "VTOMAN"
    # Unknown all-caps token is accepted when not a seller-code pattern.
    assert parse.brand_from_title("AFERIY P280 Portable Power Station", []) == "AFERIY"
    # A seller-code-shaped first token is rejected, not promoted to brand.
    assert parse.brand_from_title("HVSYVVSRL Random Product Title", []) is None
    # Empty / no caps -> None.
    assert parse.brand_from_title("", []) is None
    assert parse.brand_from_title("portable charger", []) is None


def test_clean_brand_rejects_seller_codes():
    # Amazon falls back to a random-looking seller code for products with no
    # registered brand. These should be rejected so the brand-reputation model
    # doesn't aggregate them as a real vendor.
    assert parse.clean_brand("HVSYVVSRL") is None     # 0 vowels, 9 chars
    assert parse.clean_brand("HIJYMNZPQ") is None     # 1 vowel (I), 9 chars
    assert parse.clean_brand("FNFDKDK") is None       # 0 vowels, 7 chars
    # Real all-caps brand names must still pass.
    assert parse.clean_brand("INIU") == "INIU"
    assert parse.clean_brand("UGREEN") == "UGREEN"
    assert parse.clean_brand("AUKEY") == "AUKEY"
    assert parse.clean_brand("ANKER") == "ANKER"
    # Mixed-case strings are not seller codes by definition.
    assert parse.clean_brand("Brand: NoName") == "NoName"


def test_asin_from_url():
    assert parse.asin_from_url("https://www.amazon.co.uk/dp/B0ABCD1234?th=1") == "B0ABCD1234"
    assert parse.asin_from_url("/gp/product/B07XYZ8901/") == "B07XYZ8901"


def test_is_battery_bank():
    assert parse.is_battery_bank("Power Bank Fast Charging 27000mAh, Portable")
    assert parse.is_battery_bank("INIU Portable Charger 20000mAh")
    # accessories are rejected even if they mention a battery
    assert not parse.is_battery_bank("Phone Case with 5000mAh Battery", 5000)
    assert not parse.is_battery_bank("USB-C Charging Cable 2m")
    assert not parse.is_battery_bank("Tempered Glass Screen Protector")
    assert not parse.is_battery_bank("Silicone Cover for iPhone 15")
    # a power bank that ships with a case is still a power bank
    assert parse.is_battery_bank("Power Bank 20000mAh with Carry Case")
    # real power banks that bundle a cable / mention compatibility must be kept
    assert parse.is_battery_bank(
        "INIU Power Bank 10000mAh 22.5W Fast Charging with USB C Cable", 10000)
    assert parse.is_battery_bank(
        "CUKTECH CP13 Magsafe Power Bank Qi2 15W Max Wireless Charger", 5000)
    assert parse.is_battery_bank(
        "Anker Power Bank 20000mAh, 30W, USB-C Charging Cable included", 20000)
    # but a standalone cable / charger (no power-bank phrase) is dropped
    assert not parse.is_battery_bank("USB C to USB C Charging Cable 2m")
    assert not parse.is_battery_bank("65W USB-C Wall Charger Adapter")
    # a CASE *for* a power bank must be rejected even though it says "power bank"
    assert not parse.is_battery_bank(
        "khanka Hard Travel Case Replacement for Anker Power Bank 40000mAh", 40000)
    assert not parse.is_battery_bank(
        "Khanka (Case only) Hard Carrying Case for RAVPower Power Bank", 20000)
    # unknown title but real capacity -> keep
    assert parse.is_battery_bank("", 10000)
    assert not parse.is_battery_bank("", None)
    # mAh in title alone (no explicit power-bank keyword) is a positive signal
    assert parse.is_battery_bank("INIU 10000mAh Slim, 22.5W Fast Charging")
    assert parse.is_battery_bank("20000mAh Li-ion Fast Charge Bank")


def test_is_power_station_high_wh():
    # ≥200Wh in title classifies as power station even without keyword phrase
    assert parse.is_power_station("Jackery Explorer 500Wh")
    assert parse.is_power_station("Anker SOLIX C1000 1056Wh")
    # Sub-200Wh or no Wh -> not classified as power station by Wh alone
    assert not parse.is_power_station("INIU 10000mAh Slim")
    assert not parse.is_power_station("USB-C Cable 1m")
    # Keyword phrase still works
    assert parse.is_power_station("EcoFlow DELTA 2 Portable Power Station 1024Wh")


def test_extract_ac_output_w_unlabelled_power_station():
    # Wattage in title without "AC output" keyword — common in power station names.
    assert parse.extract_ac_output_w(
        "Anker SOLIX C1000 Portable Power Station, 1800W (Peak 2400W)") == 1800
    assert parse.extract_ac_output_w(
        "DJI Power 1000 V2 Portable Power Station, 2600W Stable Output") == 2600
    # Explicit label still works.
    assert parse.extract_ac_output_w(
        "EcoFlow DELTA 2 Portable Power Station 1024Wh, 1800W AC Output") == 1800
    # Non-station product must not get a spurious value.
    assert parse.extract_ac_output_w("PowerBank 10000mAh USB-C") is None
