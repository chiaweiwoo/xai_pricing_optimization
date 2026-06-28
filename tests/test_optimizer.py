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


def test_optimizer_enforces_valid_discount_lock(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="lock_enforced_test",
            scenario_name="Lock Enforced Test",
        ),
    )

    forced_upc, forced_discount_pct = conn.execute(
        """
        SELECT upc, discount_pct
        FROM candidate_outcomes
        WHERE scenario_id = ?
          AND upc = '1001'
          AND is_hard_valid = 1
          AND discount_pct > 0
        ORDER BY discount_pct DESC
        LIMIT 1
        """,
        ("lock_enforced_test",),
    ).fetchone()

    optimizer = PricingOptimizer(conn)
    result = optimizer.solve(
        SolveRequest(
            scenario_id="lock_enforced_test",
            exact_discount_locks={forced_upc: float(forced_discount_pct)},
        )
    )

    assert result.status == "optimal"
    selected = {row["upc"]: row for row in result.selections}
    assert selected[forced_upc]["discount_pct"] == round(float(forced_discount_pct), 4)


def test_optimizer_respects_zero_budget_override(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="zero_budget_test",
            scenario_name="Zero Budget Test",
        ),
    )

    optimizer = PricingOptimizer(conn)
    result = optimizer.solve(SolveRequest(scenario_id="zero_budget_test", budget_pct=0.0))

    assert result.status == "optimal"
    assert result.summary["budget_limit_pct"] == 0.0
    assert result.summary["promoted_products"] == 0
    assert all(row["discount_pct"] == 0.0 for row in result.selections)


def test_optimizer_rejects_duplicate_run_id(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="immutability_test",
            scenario_name="Immutability Test",
        ),
    )

    optimizer = PricingOptimizer(conn)
    first = optimizer.solve(SolveRequest(scenario_id="immutability_test", run_id="official_run"))

    assert first.status == "optimal"

    try:
        optimizer.solve(SolveRequest(scenario_id="immutability_test", run_id="official_run"))
    except RuntimeError as exc:
        assert "cannot be overwritten" in str(exc)
    else:
        raise AssertionError("Expected duplicate run_id to be rejected")


def test_optimizer_rejects_invalid_budget_range(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="invalid_budget_test",
            scenario_name="Invalid Budget Test",
        ),
    )

    optimizer = PricingOptimizer(conn)

    try:
        optimizer.solve(SolveRequest(scenario_id="invalid_budget_test", budget_pct=1.25))
    except ValueError as exc:
        assert "budget_pct" in str(exc)
    else:
        raise AssertionError("Expected invalid budget_pct to be rejected")


def test_optimizer_reports_portfolio_budget_conflict_in_precheck(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="budget_conflict_test",
            scenario_name="Budget Conflict Test",
        ),
    )

    forced_row = conn.execute(
        """
        SELECT upc, discount_pct
        FROM candidate_outcomes
        WHERE scenario_id = ?
          AND upc = '1001'
          AND is_hard_valid = 1
          AND discount_pct > 0
        ORDER BY discount_pct DESC
        LIMIT 1
        """,
        ("budget_conflict_test",),
    ).fetchone()

    optimizer = PricingOptimizer(conn)
    result = optimizer.solve(
        SolveRequest(
            scenario_id="budget_conflict_test",
            budget_pct=0.0,
            exact_discount_locks={forced_row["upc"]: float(forced_row["discount_pct"])},
        )
    )

    assert result.status == "infeasible_precheck"
    assert result.diagnostics["precheck"]["global_conflicts"][0]["reason"] == "portfolio_budget"
