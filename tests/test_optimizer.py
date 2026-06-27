from xai_pricing.optimizer import PricingOptimizer, SolveRequest
from xai_pricing.synthetic import ScenarioSettings, build_demo_scenario


def test_optimizer_solves_balanced_scenario(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="balanced_test",
            scenario_name="Balanced Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    optimizer = PricingOptimizer(conn)
    result = optimizer.solve(SolveRequest(scenario_id="balanced_test"))

    assert result.status == "optimal"
    assert len(result.phases) == 3
    assert result.summary["selected_products"] == 8
    assert result.summary["promoted_products"] > 0
    assert result.summary["budget_utilization_pct"] <= 0.1001

    stored_status = conn.execute(
        "SELECT status FROM optimizer_runs WHERE run_id = ?",
        (result.run_id,),
    ).fetchone()[0]
    assert stored_status == "optimal"


def test_optimizer_rejects_missing_lock_candidate(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="lock_test",
            scenario_name="Lock Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    optimizer = PricingOptimizer(conn)
    result = optimizer.solve(
        SolveRequest(
            scenario_id="lock_test",
            exact_discount_locks={"1001": 0.30},
        )
    )

    assert result.status == "infeasible_precheck"
    assert result.diagnostics["precheck"]["lock_conflicts"][0]["reason"] == "candidate_missing"
