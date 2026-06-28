from xai_pricing.ui_support import build_selection_snapshot, select_review_cases


def test_select_review_cases_is_deterministic_and_unique() -> None:
    official = [
        {
            "upc": "A",
            "product_label": "Alpha",
            "role": "kvi",
            "archetype": "competitor_pressure",
            "discount_pct": 0.10,
            "gross_profit": 90.0,
            "competitor_gap": 0.2,
            "ending_inventory_units": 30.0,
        },
        {
            "upc": "B",
            "product_label": "Beta",
            "role": "margin_driver",
            "archetype": "low_inventory",
            "discount_pct": 0.00,
            "gross_profit": 120.0,
            "competitor_gap": 1.8,
            "ending_inventory_units": 11.0,
        },
        {
            "upc": "C",
            "product_label": "Gamma",
            "role": "traffic_driver",
            "archetype": "promotion_opportunity",
            "discount_pct": 0.15,
            "gross_profit": 80.0,
            "competitor_gap": 0.7,
            "ending_inventory_units": 40.0,
        },
    ]
    current = [
        {"upc": "A", "discount_pct": 0.00, "gross_profit": 100.0, "competitor_gap": 3.5},
        {"upc": "B", "discount_pct": 0.05, "gross_profit": 122.0, "competitor_gap": 2.1},
        {"upc": "C", "discount_pct": 0.00, "gross_profit": 110.0, "competitor_gap": 1.1},
    ]
    catalog = [
        {"upc": "A", "product_label": "Alpha", "category": "Cold", "role": "kvi", "archetype": "competitor_pressure", "on_hand_units": 100.0, "inbound_units": 0.0, "safety_stock_units": 10.0},
        {"upc": "B", "product_label": "Beta", "category": "Cold", "role": "margin_driver", "archetype": "low_inventory", "on_hand_units": 20.0, "inbound_units": 0.0, "safety_stock_units": 10.0},
        {"upc": "C", "product_label": "Gamma", "category": "Cold", "role": "traffic_driver", "archetype": "promotion_opportunity", "on_hand_units": 120.0, "inbound_units": 0.0, "safety_stock_units": 15.0},
    ]

    snapshot = build_selection_snapshot(official, current, catalog)
    cases = select_review_cases(snapshot)

    assert [case["case_id"] for case in cases] == [
        "competitor_response",
        "inventory_protection",
        "gross_profit_tradeoff",
    ]
    assert cases[0]["upc"] == "A"
    assert cases[1]["upc"] == "B"
    assert cases[2]["upc"] == "C"
    assert len({case["upc"] for case in cases}) == 3
