from xai_pricing.conversation import PricingConversationService
from xai_pricing.planner import PricingDecisionService
from xai_pricing.synthetic import ScenarioSettings, build_demo_scenario


def test_conversation_supports_plan_summary_without_llm(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="conversation_summary_test",
            scenario_name="Conversation Summary Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    planner = PricingDecisionService(conn)
    plan = planner.build_plan_bundle("conversation_summary_test")
    service = PricingConversationService(planner)

    turn = service.handle_question(plan, "summarize the proposal")

    assert turn.intent["intent"] == "PLAN_SUMMARY"
    assert turn.evidence["official_run_id"] == plan.official.run_id
    assert "gross profit" in turn.response_text.lower()


def test_conversation_runs_counterfactual_without_mutating_official(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="conversation_what_if_test",
            scenario_name="Conversation What If Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    planner = PricingDecisionService(conn)
    plan = planner.build_plan_bundle("conversation_what_if_test")
    service = PricingConversationService(planner)
    upc = plan.official.selections[0]["upc"]
    current_discount = int(round(plan.official.selections[0]["discount_pct"] * 100))
    requested_discount = 0 if current_discount != 0 else 5

    turn = service.handle_question(plan, f"what if we force {requested_discount}% for sku {upc}?")

    assert turn.intent["intent"] == "OVERRIDE_WHAT_IF"
    assert turn.evidence["source_run_id"] == plan.official.run_id
    assert turn.evidence["what_if_run_id"] != plan.official.run_id
    assert "official proposal unchanged" in turn.response_text.lower()


def test_conversation_reports_infeasible_override_cleanly(seeded_conn) -> None:
    conn, dataset_version_id = seeded_conn
    build_demo_scenario(
        conn,
        dataset_version_id,
        ScenarioSettings(
            scenario_id="conversation_infeasible_test",
            scenario_name="Conversation Infeasible Test",
            profile_id="balanced_campaign_v1",
        ),
    )

    planner = PricingDecisionService(conn)
    plan = planner.build_plan_bundle("conversation_infeasible_test")
    service = PricingConversationService(planner)

    turn = service.handle_question(plan, "what if we force 30% for sku 1001?")

    assert turn.intent["intent"] == "OVERRIDE_WHAT_IF"
    assert turn.evidence["comparison"]["comparable"] is False
    assert "infeasible" in turn.response_text.lower()
    assert "official proposal unchanged" in turn.response_text.lower()
