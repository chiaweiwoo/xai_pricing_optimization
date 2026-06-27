# Data Layer

The data layer is intentionally split into five rerunnable scripts:

1. `download_data.py`
2. `ingest.py`
3. `validate_data.py`
4. `generate_demo_context.py`
5. `profile_data.py`

## What is public vs synthetic

Public source:

- dunnhumby Breakfast at the Frat workbook
- store lookup
- product lookup
- weekly store x SKU sales, spend, price, base price, and promo flags

Synthetic but deterministic:

- unit cost
- competitor price
- inventory
- strategic role
- elasticity-backed demand model parameters
- candidate price outcomes for optimization

Synthetic records are always written with `origin = 'synthetic'`.

## Database shape

Observed layer:

- `stores`
- `products`
- `weekly_sales`
- `v_valid_weekly_sales`
- `v_chain_sku_week`

Scenario layer:

- `scenarios`
- `product_context`
- `competitor_prices`
- `inventory_positions`
- `demand_models`
- `candidate_outcomes`
- `product_relationships`

Governance layer:

- `schema_migrations`
- `dataset_versions`
- `ingestion_runs`
- `quality_checks`
