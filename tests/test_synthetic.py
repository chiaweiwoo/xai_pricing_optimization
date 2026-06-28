from xai_pricing.synthetic import ScenarioSettings, build_demo_scenario


def test_build_demo_scenario_produces_feasible_candidates(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn

    stats = build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="synthetic_test",
            scenario_name="Synthetic Test",
        ),
    )

    assert stats["products"] == 8
    assert stats["candidate_outcomes"] == 48
    invalid_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT upc
            FROM candidate_outcomes
            WHERE scenario_id = ?
            GROUP BY upc
            HAVING SUM(is_hard_valid) = 0
        )
        """,
        ("synthetic_test",),
    ).fetchone()[0]
    assert invalid_count == 0
