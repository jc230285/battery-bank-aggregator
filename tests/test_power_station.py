"""Unit tests for power-station parsers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parse


def test_is_power_station():
    assert parse.is_power_station("Jackery Explorer 1000 Portable Power Station")
    assert parse.is_power_station("EcoFlow Solar Generator 600W LiFePO4")
    assert not parse.is_power_station("Anker Power Bank 20000mAh")
    assert not parse.is_power_station("Carry Case for Bluetti Power Station")  # accessory
    assert not parse.is_power_station("")


def test_extract_chemistry():
    assert parse.extract_chemistry("LiFePO4 1024Wh power station") == "lifepo4"
    assert parse.extract_chemistry("Lithium Iron Phosphate battery") == "lifepo4"
    assert parse.extract_chemistry("20000mAh Li-polymer power bank") == "li-po"
    assert parse.extract_chemistry("NMC ternary lithium pack") == "nmc"
    assert parse.extract_chemistry("Lithium-ion portable charger") == "li-ion"
    assert parse.extract_chemistry("no chemistry mentioned") is None


def test_extract_wh():
    assert parse.extract_wh("Capacity 1024Wh") == 1024
    assert parse.extract_wh("1.5kWh LiFePO4") == 1500
    assert parse.extract_wh("500 Wh power station") == 500
    assert parse.extract_wh("just 20000mAh, no Wh") is None


def test_extract_ac_output_w():
    assert parse.extract_ac_output_w("Pure Sine Wave 1000W AC Output") == 1000
    assert parse.extract_ac_output_w("2000W surge, 1000W rated output") == 1000
    assert parse.extract_ac_output_w("USB-C 100W only, no mains") is None  # no AC context


def test_extract_ac_sockets():
    assert parse.extract_ac_sockets("2 AC outlets and 3 USB ports") == 2
    assert parse.extract_ac_sockets("AC socket x 3") == 3
    assert parse.extract_ac_sockets("1 AC output") == 1
    assert parse.extract_ac_sockets("no mains here") is None


def test_extract_solar_input_and_cycles():
    assert parse.extract_solar_input_w("Solar input up to 200W via MPPT") == 200
    assert parse.extract_solar_input_w("no solar support") is None
    assert parse.extract_cycle_life("3000+ charge cycles") == 3000
    assert parse.extract_cycle_life("rated 3,500 cycles") == 3500


def test_detect_features_expandable_ups():
    f = parse.detect_features("Expandable capacity, UPS function, LCD display")
    assert f["expandable"] and f["ups"] and f["display"]
    # word-boundary: 'backups' must not trigger ups
    assert not parse.detect_features("cloud backups included")["ups"]
