import pytest

from xai_pricing.demand import ConstantElasticityDemandModel


def test_constant_elasticity_reference_price_returns_baseline() -> None:
    model = ConstantElasticityDemandModel(
        reference_price=4.0,
        baseline_units=100.0,
        elasticity=-1.5,
        uncertainty_pct=0.2,
    )
    estimate = model.predict(4.0)

    assert estimate.mean_units == pytest.approx(100.0)
    assert estimate.lower_units == pytest.approx(80.0)
    assert estimate.upper_units == pytest.approx(120.0)


def test_constant_elasticity_is_monotone_for_negative_elasticity() -> None:
    model = ConstantElasticityDemandModel(
        reference_price=5.0,
        baseline_units=80.0,
        elasticity=-2.0,
        uncertainty_pct=0.1,
    )

    cheaper = model.predict(4.5)
    pricier = model.predict(5.5)

    assert cheaper.mean_units > pricier.mean_units
