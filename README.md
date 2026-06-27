# XAI Pricing Optimization

An explainable retail pricing decision system. Given precomputed demand/price-response curves for multiple SKUs, it selects a portfolio price plan that maximizes expected gross profit while satisfying business rules, then makes every recommendation reviewable through solver-grounded explanations and editable what-if scenarios.

The repository now has a working data-layer foundation:

- verified public-data download
- SQLite schema with tracked migrations
- workbook-to-database ingest
- persisted data quality checks
- deterministic synthetic pricing context
- generated EDA/profile artifacts

See the [PRD](docs/PRD.md), [data layer notes](docs/data_layer.md), and [synthetic methodology](docs/synthetic_methodology.md).

## Quickstart

```bash
uv sync --dev
uv run python download_data.py
uv run python ingest.py
uv run python validate_data.py
uv run python generate_demo_context.py
uv run python profile_data.py
uv run pytest
```

Outputs:

- SQLite database: `db/pricing_optimization.db`
- Raw source files: `data/raw/dunnhumby_breakfast_at_the_frat/`
- Profiling artifacts: `reports/generated/`

## Core business questions

- What price or discount should we set for every SKU in the next pricing cycle?
- Which business rules shaped each recommendation?
- Why was this discount selected instead of the current price or the next-best candidate?
- What happens to volume, revenue, gross profit, and the rest of the portfolio if a planner overrides one price?
- Why is a requested set of rules infeasible, and what is the smallest useful relaxation?

## Proposed MVP

The MVP is a single-period portfolio optimizer. It deliberately starts after demand forecasting: its input is a table of allowed price points and the expected demand at each point.

```text
product + price-response inputs + competitor/inventory context
                            |
                            v
                  validated decision model
                            |
                            v
             discrete constrained optimization
                            |
                            v
        plan + metrics + rule evidence + alternatives
                            |
                            v
             grounded LLM explanation / rule UX
```

The solver, not the LLM, owns calculations and feasibility. The LLM may translate a planner's request into a typed rule proposal and narrate structured solver evidence; it may not invent constraints, numbers, or causal claims.

## Initial data direction

Use a small, reproducible retail portfolio derived from public data, then add clearly labelled synthetic fields that public datasets normally omit:

- Historical product/store/week price and units: either the M5 Walmart data already used in the sibling `xai_demand_forecasting` project, or dunnhumby's **Breakfast at the Frat** dataset.
- Assumed optimizer input: baseline demand and own-price elasticity (or a precomputed demand value for every allowed price point).
- Synthetic but deterministic: unit cost, inventory, competitor price, strategic role, minimum margin, price ladder, and price-change limits.

The current recommendation is **Breakfast at the Frat for the MVP story**, because it was designed for price-sensitivity and promotional analysis and includes base price, shelf price, weekly unit sales, and promotion support. M5 remains the fallback when continuity with the demand-forecasting project matters more than pricing richness.

## Local references

- `../xai_demand_forecasting`: repository structure, staged/re-runnable pipeline, tests, SQLite artifacts, and evidence-grounded narrative patterns.
- `../dfs-ai-pricing`: price-point enumeration, margin/discount bounds, inventory filtering, portfolio optimization, fixed-price overrides, competitor inputs, and a demand-curve simulator. It is reference material, not a code base to copy wholesale.
- [Alpha-Z pricing / decision-system framing](https://www.alpha-z.io/applications/pricing): formulate, solve, explain, and retain reusable decision knowledge.

## Research references

- [dunnhumby Source Files](https://www.dunnhumby.com/source-files/) — public retail datasets and the Breakfast at the Frat description.
- [PepsiCo PricingAI and PromoAI case study](https://arxiv.org/abs/2606.17941) — own/cross-price elasticity, competitor interactions, customizable constraints, price ladders, uncertainty, and adjusted scenarios.
- [Retail SKU promotion optimization](https://eprints.lancs.ac.uk/id/eprint/83834/) — category-level profit optimization with operational business rules.

## Status

- [x] Git repository initialized
- [x] Initial product and engineering guidance
- [x] PRD v0.1 with assumptions and open decisions
- [x] Public dataset selected, downloaded, and ingested into SQLite
- [x] Data profiling, validation checks, and generated EDA artifacts
- [x] Deterministic synthetic context for pricing optimization inputs
- [ ] Freeze the solver input/output contracts
- [ ] Implement a deterministic baseline solver
- [ ] Add explanation, rule-authoring, and what-if workflows
