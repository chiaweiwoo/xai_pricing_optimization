ALTER TABLE scenarios ADD COLUMN scenario_kind TEXT NOT NULL DEFAULT 'official';
ALTER TABLE scenarios ADD COLUMN parent_scenario_id TEXT;
ALTER TABLE scenarios ADD COLUMN profile_id TEXT;
ALTER TABLE scenarios ADD COLUMN budget_pct REAL NOT NULL DEFAULT 0.10;
ALTER TABLE scenarios ADD COLUMN safety_stock_pct REAL NOT NULL DEFAULT 0.25;

ALTER TABLE product_context ADD COLUMN archetype TEXT;
ALTER TABLE product_context ADD COLUMN competitor_tolerance_pct REAL NOT NULL DEFAULT 0.05;
ALTER TABLE product_context ADD COLUMN competitor_weight INTEGER NOT NULL DEFAULT 1;
ALTER TABLE product_context ADD COLUMN list_price REAL;

ALTER TABLE inventory_positions ADD COLUMN safety_stock_units REAL NOT NULL DEFAULT 0;

ALTER TABLE candidate_outcomes ADD COLUMN list_price REAL NOT NULL DEFAULT 0;
ALTER TABLE candidate_outcomes ADD COLUMN markdown_investment REAL NOT NULL DEFAULT 0;
ALTER TABLE candidate_outcomes ADD COLUMN ending_inventory_units REAL NOT NULL DEFAULT 0;
ALTER TABLE candidate_outcomes ADD COLUMN expected_lost_units REAL NOT NULL DEFAULT 0;
ALTER TABLE candidate_outcomes ADD COLUMN optimistic_lost_units REAL NOT NULL DEFAULT 0;
ALTER TABLE candidate_outcomes ADD COLUMN competitor_gap REAL NOT NULL DEFAULT 0;
ALTER TABLE candidate_outcomes ADD COLUMN is_hard_valid INTEGER NOT NULL DEFAULT 1;
ALTER TABLE candidate_outcomes ADD COLUMN hard_violation_reason TEXT;

CREATE TABLE IF NOT EXISTS optimizer_runs (
    run_id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    source_run_id TEXT,
    run_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    solver_name TEXT NOT NULL,
    objective_mode TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    budget_pct REAL NOT NULL,
    safety_stock_pct REAL NOT NULL,
    diagnostics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id),
    FOREIGN KEY (source_run_id) REFERENCES optimizer_runs(run_id)
);

CREATE TABLE IF NOT EXISTS optimizer_run_phases (
    run_id TEXT NOT NULL,
    phase_name TEXT NOT NULL,
    status TEXT NOT NULL,
    objective_value REAL,
    duration_ms INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY (run_id, phase_name),
    FOREIGN KEY (run_id) REFERENCES optimizer_runs(run_id)
);

CREATE TABLE IF NOT EXISTS optimizer_run_items (
    run_id TEXT NOT NULL,
    upc TEXT NOT NULL,
    candidate_rank INTEGER NOT NULL,
    candidate_price REAL NOT NULL,
    discount_pct REAL NOT NULL,
    expected_units REAL NOT NULL,
    revenue REAL NOT NULL,
    gross_profit REAL NOT NULL,
    markdown_investment REAL NOT NULL,
    ending_inventory_units REAL NOT NULL,
    competitor_index REAL,
    competitor_gap REAL NOT NULL,
    selection_rank INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (run_id, upc),
    FOREIGN KEY (run_id) REFERENCES optimizer_runs(run_id),
    FOREIGN KEY (upc) REFERENCES products(upc)
);
