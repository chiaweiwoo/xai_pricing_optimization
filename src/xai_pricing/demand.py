from dataclasses import dataclass


@dataclass(frozen=True)
class DemandEstimate:
    mean_units: float
    lower_units: float
    upper_units: float


@dataclass(frozen=True)
class ConstantElasticityDemandModel:
    reference_price: float
    baseline_units: float
    elasticity: float
    uncertainty_pct: float

    def predict(self, candidate_price: float) -> DemandEstimate:
        if candidate_price <= 0:
            raise ValueError("candidate_price must be positive")
        ratio = candidate_price / self.reference_price
        mean_units = max(self.baseline_units * (ratio ** self.elasticity), 0.0)
        spread = mean_units * self.uncertainty_pct
        return DemandEstimate(
            mean_units=mean_units,
            lower_units=max(mean_units - spread, 0.0),
            upper_units=mean_units + spread,
        )
