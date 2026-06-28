from xai_pricing.planner import PricingDecisionService
from xai_pricing.synthetic import ScenarioSettings, build_demo_scenario


def test_plan_bundle_provides_benchmarks(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="planner_bundle_test",
            scenario_name="Planner Bundle Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    service = PricingDecisionService(conn)
    bundle = service.build_plan_bundle("planner_bundle_test")

    assert bundle.official.status == "optimal"
    assert bundle.profit_first.status == "optimal"
    assert bundle.current_price.status in {"ready", "violations_detected"}
    assert bundle.current_price.summary["selected_products"] == bundle.official.summary["selected_products"]
    assert (
        bundle.theoretical_ceiling.summary["total_gross_profit"]
        >= bundle.official.summary["total_gross_profit"]
    )
    assert "role" in bundle.official.selections[0]
    assert "archetype" in bundle.official.selections[0]


def test_counterfactual_runs_are_cached_and_immutable(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="planner_counterfactual_test",
            scenario_name="Planner Counterfactual Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    service = PricingDecisionService(conn)
    bundle = service.build_plan_bundle("planner_counterfactual_test")
    source_run_id = bundle.official.run_id

    target_row = conn.execute(
        """
        SELECT c.upc, c.discount_pct
        FROM candidate_outcomes c
        JOIN optimizer_run_items i
          ON i.upc = c.upc
         AND i.run_id = ?
        WHERE c.scenario_id = ?
          AND c.upc = i.upc
          AND c.is_hard_valid = 1
          AND ABS(c.discount_pct - i.discount_pct) > 1e-9
        ORDER BY c.discount_pct DESC
        LIMIT 1
        """,
        (source_run_id, "planner_counterfactual_test"),
    ).fetchone()

    counterfactual = service.simulate_counterfactual(
        source_run_id,
        exact_discount_locks={target_row["upc"]: float(target_row["discount_pct"])},
    )
    rerun = service.simulate_counterfactual(
        source_run_id,
        exact_discount_locks={target_row["upc"]: float(target_row["discount_pct"])},
    )

    assert counterfactual.result.status == "optimal"
    assert counterfactual.result.run_id != source_run_id
    assert counterfactual.comparison["base_run_id"] == source_run_id
    assert rerun.cached is True

    stored_source = conn.execute(
        "SELECT source_run_id, run_kind FROM optimizer_runs WHERE run_id = ?",
        (counterfactual.result.run_id,),
    ).fetchone()
    assert stored_source["source_run_id"] == source_run_id
    assert stored_source["run_kind"] == "what_if"


def test_counterfactual_infeasibility_returns_diagnostics_not_fake_deltas(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="planner_infeasible_cf_test",
            scenario_name="Planner Infeasible CF Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    service = PricingDecisionService(conn)
    bundle = service.build_plan_bundle("planner_infeasible_cf_test")
    counterfactual = service.simulate_counterfactual(
        bundle.official.run_id,
        exact_discount_locks={"1001": 0.30},
    )

    assert counterfactual.result.status == "infeasible_precheck"
    assert counterfactual.comparison["comparable"] is False
    assert counterfactual.comparison["summary_delta"] is None
    assert counterfactual.comparison["infeasibility"]["lock_conflicts"][0]["reason"] == "candidate_missing"


def test_sku_dossier_exposes_selected_current_and_local_best(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="planner_dossier_test",
            scenario_name="Planner Dossier Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    service = PricingDecisionService(conn)
    bundle = service.build_plan_bundle("planner_dossier_test")
    upc = bundle.official.selections[0]["upc"]
    dossier = service.get_sku_dossier(bundle.official.run_id, upc)

    assert dossier["selected"]["candidate_rank"] >= 1
    assert dossier["current"]["candidate_rank"] >= 1
    assert dossier["local_best_feasible"]["candidate_rank"] >= 1
    assert any(item["is_selected"] for item in dossier["alternatives"])
