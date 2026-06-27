from xai_pricing.synthetic import PROFILE_CONFIG, ScenarioSettings, build_demo_scenario


def test_build_demo_scenarios_produce_feasible_candidates(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn

    for profile_id, profile in PROFILE_CONFIG.items():
        stats = build_demo_scenario(
            conn,
            dataset_version_id,
            ScenarioSettings(
                scenario_id=profile_id,
                scenario_name=profile["scenario_name"],
                profile_id=profile_id,
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
            (profile_id,),
        ).fetchone()[0]
        assert invalid_count == 0
