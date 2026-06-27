CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id TEXT PRIMARY KEY,
    scenario_name TEXT NOT NULL,
    dataset_version_id TEXT NOT NULL,
    planning_week_end TEXT NOT NULL,
    seed INTEGER NOT NULL,
    objective TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (dataset_version_id) REFERENCES dataset_versions(dataset_version_id)
);

CREATE TABLE IF NOT EXISTS product_context (
    scenario_id TEXT NOT NULL,
    upc TEXT NOT NULL,
    strategic_role TEXT NOT NULL,
    current_price REAL NOT NULL,
    reference_price REAL NOT NULL,
    unit_cost REAL NOT NULL,
    min_margin_pct REAL NOT NULL,
    max_discount_pct REAL NOT NULL,
    role_rank INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (scenario_id, upc),
    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id),
    FOREIGN KEY (upc) REFERENCES products(upc)
);

CREATE TABLE IF NOT EXISTS competitor_prices (
    scenario_id TEXT NOT NULL,
    upc TEXT NOT NULL,
    competitor_name TEXT NOT NULL,
    competitor_price REAL NOT NULL,
    competitor_index REAL,
    origin TEXT NOT NULL,
    PRIMARY KEY (scenario_id, upc),
    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id),
    FOREIGN KEY (upc) REFERENCES products(upc)
);

CREATE TABLE IF NOT EXISTS inventory_positions (
    scenario_id TEXT NOT NULL,
    upc TEXT NOT NULL,
    on_hand_units INTEGER NOT NULL,
    inbound_units INTEGER NOT NULL,
    weeks_of_cover REAL NOT NULL,
    sell_through_target_pct REAL NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (scenario_id, upc),
    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id),
    FOREIGN KEY (upc) REFERENCES products(upc)
);

CREATE TABLE IF NOT EXISTS demand_models (
    scenario_id TEXT NOT NULL,
    upc TEXT NOT NULL,
    model_type TEXT NOT NULL,
    reference_price REAL NOT NULL,
    baseline_units REAL NOT NULL,
    elasticity REAL NOT NULL,
    uncertainty_pct REAL NOT NULL,
    params_json TEXT NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (scenario_id, upc),
    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id),
    FOREIGN KEY (upc) REFERENCES products(upc)
);

CREATE TABLE IF NOT EXISTS candidate_outcomes (
    scenario_id TEXT NOT NULL,
    upc TEXT NOT NULL,
    candidate_rank INTEGER NOT NULL,
    candidate_price REAL NOT NULL,
    discount_pct REAL NOT NULL,
    expected_units REAL NOT NULL,
    conservative_units REAL NOT NULL,
    optimistic_units REAL NOT NULL,
    revenue REAL NOT NULL,
    gross_profit REAL NOT NULL,
    gross_margin_pct REAL NOT NULL,
    competitor_index REAL,
    inventory_cap_units INTEGER NOT NULL,
    expected_units_capped REAL NOT NULL,
    is_current_price INTEGER NOT NULL,
    is_reference_price INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (scenario_id, upc, candidate_rank),
    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id),
    FOREIGN KEY (upc) REFERENCES products(upc)
);

CREATE TABLE IF NOT EXISTS product_relationships (
    scenario_id TEXT NOT NULL,
    relationship_group_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    upc TEXT NOT NULL,
    relation_order INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (scenario_id, relationship_group_id, upc),
    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id),
    FOREIGN KEY (upc) REFERENCES products(upc)
);
