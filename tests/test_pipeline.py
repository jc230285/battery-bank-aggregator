"""Integration tests for the analysis pipeline against an in-memory database."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import analysis


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def test_run_analysis_pipeline():
    s = _session()
    for i in range(14):
        s.add(models.Product(
            asin=f"A{i:02d}", title=f"Power Bank {10000 + i * 2000}mAh",
            brand="Anker" if i % 3 == 0 else "NoName",
            price=15.0 + i * 2.5, claimed_mah=10000 + i * 2000, weight_g=200 + i * 10,
            usb_c=1, usb_a=2, pd_w=20, wireless=(i % 2 == 0), rating=4.0 + (i % 5) * 0.1,
        ))
    s.commit()

    out = analysis.run_analysis(s)        # {category: model}
    prods = s.query(models.Product).all()
    model = out["power_bank"]

    assert all(p.cost_per_mah and p.cost_per_mah > 0 for p in prods)
    assert all(p.capacity_wh and p.capacity_wh > 0 for p in prods)  # derived from mAh
    assert all(p.honesty and p.honesty.get("brand") is not None for p in prods)
    assert model and all(v >= 0 for v in model["coef"].values())  # NNLS non-negative
    assert all(all(v >= 0 for v in (p.feature_contrib or {}).values()) for p in prods)
    assert s.get(models.Meta, "feature_model") is not None


def test_power_station_pipeline():
    s = _session()
    for i in range(14):
        s.add(models.Product(
            asin=f"PS{i:02d}", title=f"LiFePO4 Power Station {500 + i * 100}Wh",
            category="power_station", brand="EcoFlow" if i % 3 == 0 else "NoName",
            price=300.0 + i * 50, capacity_wh=500 + i * 100, chemistry="lifepo4",
            weight_g=6000 + i * 500, ac_output_w=1000 + i * 50, ac_sockets=2,
            solar_input_w=200, cycle_life=3000, rating=4.0 + (i % 5) * 0.1,
        ))
    s.commit()
    out = analysis.run_analysis(s)
    prods = s.query(models.Product).filter(models.Product.category == "power_station").all()
    model = out["power_station"]
    assert all(p.cost_per_wh and p.cost_per_wh > 0 for p in prods)   # Wh-based cost metric
    assert model and "ac_output_w" in model["coef"]                  # station feature set used
    assert all((p.feature_contrib or {}).get("ac_output_w", 0) >= 0 for p in prods)


def test_physics_flag_in_pipeline():
    s = _session()
    # A 40000 mAh pack at 90 g is physically impossible -> must be flagged.
    s.add(models.Product(asin="LIGHT", title="Power Bank 40000mAh", brand="NoName",
                         price=20, claimed_mah=40000, weight_g=90, rating=4.0))
    for i in range(13):  # padding so the regression has enough rows
        s.add(models.Product(asin=f"B{i:02d}", title="Power Bank 10000mAh", brand="Anker",
                             price=20 + i, claimed_mah=10000, weight_g=250, rating=4.5))
    s.commit()

    analysis.run_analysis(s)
    light = s.get(models.Product, "LIGHT")
    assert "impossible_capacity" in (light.honesty_flags or [])
    # honesty data should quantify the overstatement
    assert light.honesty.get("mah_cap")
    assert light.honesty.get("overstatement_pct", 0) > 0
    assert light.honesty["mah_cap"] < 40000  # claimed capacity exceeds the cap


def test_too_few_rows_no_model():
    s = _session()
    for i in range(3):
        s.add(models.Product(asin=f"C{i}", title="Power Bank", brand="Anker",
                             price=20, claimed_mah=10000, weight_g=250))
    s.commit()
    out = analysis.run_analysis(s)
    assert not out.get("power_bank")  # too few rows -> no fitted model ({} )


def test_backfill_fills_mah_and_wh_from_raw_specs():
    s = _session()
    # Product stored without claimed_mah/capacity_wh but with raw_specs text.
    s.add(models.Product(
        asin="BF1", title="Power Bank", brand="Anker", price=30,
        claimed_mah=None, capacity_wh=None,
        raw_specs={"bullets": "20000mAh high capacity", "details": "Weight: 320g"},
    ))
    s.add(models.Product(
        asin="BF2", title="Power Station", brand="Jackery", price=300,
        category="power_station", claimed_mah=None, capacity_wh=None,
        raw_specs={"bullets": "500Wh LiFePO4 battery", "details": ""},
    ))
    s.commit()
    analysis.run_analysis(s)
    pb = s.get(models.Product, "BF1")
    ps = s.get(models.Product, "BF2")
    assert pb.claimed_mah == 20000
    assert ps.capacity_wh == 500


def test_backfill_fills_ports_and_watts():
    """Backfill should populate port counts, ac_sockets, pd_w, and max_w from raw_specs."""
    s = _session()
    s.add(models.Product(
        asin="FF1", title="Power Bank 20000mAh", brand="Anker", price=35,
        claimed_mah=20000, weight_g=350,
        usb_a=None, usb_c=None, pd_w=None,
        raw_specs={
            "bullets": "2 USB-A and 1 USB-C ports, 22.5W PD output",
            "details": "",
        },
    ))
    s.add(models.Product(
        asin="PS2", title="Power Station 500Wh", brand="Jackery", price=400,
        category="power_station", capacity_wh=500,
        ac_sockets=None,
        raw_specs={
            "bullets": "2 AC outlets, pure sine wave 1000W",
            "details": "",
        },
    ))
    s.commit()
    analysis.run_analysis(s)
    pb = s.get(models.Product, "FF1")
    ps = s.get(models.Product, "PS2")
    assert pb.usb_a == 2
    assert pb.usb_c == 1
    assert pb.pd_w == 22.5
    assert ps.ac_sockets == 2


def test_backfill_promotes_boolean_features():
    """Boolean features that were False should be promoted to True when raw_specs say True."""
    s = _session()
    s.add(models.Product(
        asin="BF1", title="Power Bank 20000mAh", brand="Anker", price=35,
        claimed_mah=20000, weight_g=350,
        wireless=False, display=False, passthrough=False, solar=False,
        raw_specs={
            "bullets": "Qi wireless charging, LCD display, pass-through charging, solar panel input",
            "details": "",
        },
    ))
    s.commit()
    analysis.run_analysis(s)
    pb = s.get(models.Product, "BF1")
    assert pb.wireless is True
    assert pb.display is True
    assert pb.passthrough is True
    assert pb.solar is True


def test_capacity_wh_rederived_after_chemistry_backfill():
    """capacity_wh for power banks must use the backfilled chemistry, not the
    default Li-ion voltage. A LiFePO4 20000 mAh pack is 64 Wh, not 74 Wh."""
    s = _session()
    # Product stored with li-ion chemistry initially; raw_specs has lifepo4 hint.
    s.add(models.Product(
        asin="LFP1", title="LFP Power Bank 20000mAh", brand="NoName",
        price=40, claimed_mah=20000, chemistry=None,
        raw_specs={"bullets": "lithium iron phosphate cell 3.2V", "details": ""},
    ))
    s.commit()
    analysis.run_analysis(s)
    p = s.get(models.Product, "LFP1")
    # Chemistry should be backfilled to lifepo4
    assert p.chemistry == "lifepo4"
    # Wh should be derived using 3.2V, not 3.7V
    assert p.capacity_wh is not None
    assert 63 < p.capacity_wh < 65   # 20000/1000 * 3.2 = 64 Wh
