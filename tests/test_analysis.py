import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analysis


class FakeProduct:
    def __init__(self, **kw):
        defaults = dict(
            asin="A", price=None, claimed_mah=None, weight_g=None, pd_w=None,
            usb_c=None, wireless=False, display=False, passthrough=False,
            solar=False, brand=None, rating=None, review_count=None,
            honesty_flags=None, cost_per_mah=None,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def test_physics_flags_impossible_pack():
    # 148 Wh (≈40000 mAh) needs ≥ ~569 g at Li-ion 260 Wh/kg. 90 g is impossible.
    score, flag = analysis.physics_subscore(148, 90)
    assert flag == "impossible_capacity" and score < 0.95
    # A plausible 600 g pack scores full marks.
    score2, flag2 = analysis.physics_subscore(148, 600)
    assert flag2 is None and score2 == 1.0
    # Missing data / implausible stored weight -> no score.
    assert analysis.physics_subscore(None, 100) == (None, None)
    assert analysis.physics_subscore(148, 5) == (None, None)
    assert analysis.max_plausible_mah(5) is None


def test_physics_flags_impossible_without_weight():
    # 90000 mAh * 3.7 V = 333 Wh — more than a 1 kg Li-ion pack can hold (260 Wh).
    # Should be flagged even with no weight data.
    cap_90k = 90000 / 1000.0 * 3.7
    score, flag = analysis.physics_subscore(cap_90k, None)
    assert flag == "impossible_capacity"
    assert 0 < score < 1
    # 70000 mAh * 3.7 V = 259 Wh — just under the 260 Wh ceiling, no flag.
    cap_70k = 70000 / 1000.0 * 3.7
    score2, flag2 = analysis.physics_subscore(cap_70k, None)
    assert flag2 is None
    # Legitimate 20000 mAh must still pass.
    cap_20k = 20000 / 1000.0 * 3.7
    assert analysis.physics_subscore(cap_20k, None) == (None, None)


def test_physics_is_chemistry_aware():
    # 100 Wh @ 500 g: fine as Li-ion (floor ~385 g) but impossible as LiFePO4 (~625 g).
    assert analysis.physics_subscore(100, 500, "li-ion")[1] is None
    assert analysis.physics_subscore(100, 500, "lifepo4")[1] == "impossible_capacity"


def test_max_plausible_mah():
    # 250 g Li-ion at 260 Wh/kg -> 65 Wh -> ~17567 mAh at 3.7 V
    cap = analysis.max_plausible_mah(250)
    assert 17000 < cap < 18000
    assert analysis.max_plausible_mah(None) is None
    assert analysis.max_plausible_mah(0) is None


def test_max_plausible_mah_chemistry_aware():
    # LiFePO4: lower density (160 Wh/kg) AND lower voltage (3.2 V).
    # 500 g LiFePO4: 500/1000 * 160 = 80 Wh -> 80/3.2 * 1000 = 25000 mAh
    lfp = analysis.max_plausible_mah(500, "lifepo4")
    assert 24000 < lfp < 26000
    # Li-ion: 500/1000 * 260 = 130 Wh -> 130/3.7 * 1000 ≈ 35135 mAh
    lion = analysis.max_plausible_mah(500, "li-ion")
    assert 34000 < lion < 36000
    # LiFePO4 always gives fewer mAh per gram than Li-ion.
    assert lfp < lion


def test_capacity_wh_of_chemistry_aware():
    class P:
        pass
    p = P()
    p.capacity_wh = None
    p.chemistry = "lifepo4"
    p.claimed_mah = 20000
    # 20000 mAh at 3.2 V -> 64 Wh (not 74 Wh as for Li-ion at 3.7 V)
    wh = analysis.capacity_wh_of(p)
    assert 63 < wh < 65


def test_brand_subscore():
    assert analysis.brand_subscore("Anker")[0] == 1.0
    assert analysis.brand_subscore("Anker PowerCore")[0] == 1.0
    assert analysis.brand_subscore("NoNameXYZ")[0] == 0.5
    assert analysis.brand_subscore(None)[1] == "unknown_brand"


def test_brand_reputations():
    catalog = []
    # high-volume, well-rated brand (also anchors the catalog mean ~4.5)
    catalog += [FakeProduct(asin=f"g{i}", brand="BigGood", rating=4.5, review_count=1000)
                for i in range(4)]
    # same ratings but every product flagged as fake -> reputation penalised
    catalog += [FakeProduct(asin=f"f{i}", brand="FlaggedBig", rating=4.5, review_count=1000,
                            honesty_flags=["impossible_capacity"]) for i in range(4)]
    # one product, terrible rating, 1 review -> shrinks strongly toward the mean
    catalog.append(FakeProduct(asin="t0", brand="Tiny", rating=1.0, review_count=1))
    # no rating at all -> cannot be scored
    catalog.append(FakeProduct(asin="n0", brand="NoRate", rating=None))

    reps = analysis.brand_reputations(catalog)
    assert reps["biggood"] > 0.7
    assert reps["flaggedbig"] < reps["biggood"]      # fake penalty applied
    assert reps["tiny"] > 0.5                        # shrinkage rescues a 1-review brand
    assert reps["norate"] is None                    # no ratings -> unscored


def test_brand_honesty():
    reps = {"goodname": 0.8, "badname": 0.3}
    assert analysis.brand_honesty("Anker", reps) == (1.0, None)          # trusted -> no flag
    assert analysis.brand_honesty(None, reps)[1] == "unknown_brand"
    # Strong data-grounded reputation -> no flag (the data already cleared it)
    assert analysis.brand_honesty("GoodName", reps) == (0.8, None)
    # Low reputation -> flagged
    assert analysis.brand_honesty("BadName", reps) == (0.3, "unverified_brand")
    # No rep data -> default 0.5 and flagged for caution
    assert analysis.brand_honesty("Mystery", reps) == (0.5, "unverified_brand")


def test_reviews_subscore():
    score, flag = analysis.reviews_subscore(
        ["battery won't hold charge", "capacity not as advertised"])
    assert flag == "reviews_report_fake_capacity"
    assert score < 1.0
    assert analysis.reviews_subscore(["great product, fast charging"])[0] == 1.0
    # A single verbose review with many phrases should NOT flag (only 1 review affected).
    score2, flag2 = analysis.reviews_subscore(
        ["fake capacity, fake mah, doesn't hold charge, way less than advertised"])
    assert flag2 is None
    # No snippets -> no opinion.
    assert analysis.reviews_subscore([])[0] is None


def test_price_outlier_detects_cheap():
    base = [FakeProduct(asin=str(i), cost_per_mah=0.004) for i in range(10)]
    cheap = FakeProduct(asin="cheap", cost_per_mah=0.0005)
    res = analysis.price_outlier_subscores(base + [cheap])
    assert res["cheap"][1] == "too_cheap_per_capacity"
    assert res["0"][0] == 1.0


def test_self_consistency_passes_when_unclaimed():
    # No mAh or no Wh -> no opinion
    assert analysis.self_consistency_subscore(None, 100) == (None, None)
    assert analysis.self_consistency_subscore(20000, None) == (None, None)


def test_self_consistency_accepts_aligned_numbers():
    # 20000 mAh @ 3.7 V = 74 Wh (Li-ion); claimed Wh inside +-25% -> 1.0
    score, flag = analysis.self_consistency_subscore(20000, 74, "li-ion")
    assert flag is None and score == 1.0
    # LiFePO4 uses 3.2 V -> 20000 mAh = 64 Wh, claimed 64 Wh still aligned
    s2, f2 = analysis.self_consistency_subscore(20000, 64, "lifepo4")
    assert f2 is None and s2 == 1.0


def test_self_consistency_flags_inflated_wh():
    # 20000 mAh @ 3.7 V = 74 Wh; claimed 200 Wh is inflated ~2.7x -> flag set
    score, flag = analysis.self_consistency_subscore(20000, 200, "li-ion")
    assert flag == "inconsistent_capacity_claims"
    assert score < 0.5


def test_brand_mimicry_catches_typosquats_only():
    # Trusted brands and substring variants don't trigger
    assert analysis.brand_mimicry_flag("Anker") is None
    assert analysis.brand_mimicry_flag("Anker Innovations") is None
    assert analysis.brand_mimicry_flag(None) is None
    # Typosquat that doesn't contain the trusted name as a substring -> flagged
    flag = analysis.brand_mimicry_flag("Ankur")
    assert flag == "brand_mimics_anker"
    # Genuinely unrelated unknown brand -> no flag
    assert analysis.brand_mimicry_flag("ZorgBat") is None


def test_hedonic_and_value():
    # Construct a catalog where price = 5 + 1.0*kmah + 10*wireless (+ noise-free).
    products = []
    for i in range(15):
        kmah = 5 + i
        wireless = (i % 2 == 0)
        price = 5 + 1.0 * kmah + 10 * (1 if wireless else 0)
        products.append(FakeProduct(
            asin=str(i), price=price, claimed_mah=kmah * 1000,
            wireless=wireless, rating=4.0))
    model = analysis.hedonic_model(products, "power_bank")
    assert model is not None
    # capacity coefficient should be ~1 (£ per 1000 mAh).
    assert abs(model["coef"]["capacity_kmah"] - 1.0) < 0.5
    # feature_contributions are non-negative and include capacity + brand_trust.
    contrib = analysis.feature_contributions(model, products[0], "power_bank")
    assert "capacity" in contrib and contrib["capacity"] >= 0
    assert "brand_trust" in contrib
    # contrib values + intercept should sum close to fair_price.
    fp = analysis.fair_price(model, products[0], "power_bank")
    total = model["intercept"] + sum(contrib.values())
    assert abs(total - fp) < 1.0
