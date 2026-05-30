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
