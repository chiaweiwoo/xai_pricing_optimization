# AGENTS.md

Guidance for coding agents working in this repository.

## Product

Build an explainable retail pricing optimizer. The core question is:

> Why is this the best feasible portfolio price plan, what constrained it, and what changes if a planner intervenes?

Demand forecasting and elasticity estimation are out of scope for the MVP. Treat price-response inputs as versioned upstream evidence.

## Source-of-truth hierarchy

1. The solver and its persisted run artifacts own all feasibility and financial calculations.
2. Typed rule definitions own business-rule semantics.
3. Deterministic explanation builders turn solver artifacts into evidence dossiers.
4. The LLM only parses proposed rules or narrates a dossier. It must never calculate a metric, claim optimality, or infer a binding rule on its own.

## Critical invariants

- Reproducibility: the same inputs, rules, solver version, and seed produce the same plan.
- Traceability: every plan records immutable input, rule-set, objective, solver, and parent-scenario versions.
- Financial reconciliation: portfolio metrics equal the sum of selected SKU candidate metrics within an explicit tolerance.
- Exactly one allowed candidate is selected per in-scope SKU unless the run fails explicitly.
- No recommendation may violate a hard rule. Soft-rule violations must be named and costed.
- An override is a child scenario, never an in-place mutation of a completed run.
- A natural-language rule is not active until it is parsed into the typed schema, validated, previewed, and confirmed.
- Explanations quote persisted evidence. Missing evidence must be reported as unknown, not guessed.
- Synthetic data and inferred values must be visibly labelled and generated deterministically.

## Planned pipeline

Each stage should be independently re-runnable, following the useful pattern in `../xai_demand_forecasting`.

```text
ingest / generate demo inputs
    -> validate candidate and context data
    -> compile typed rules into a solver model
    -> solve and persist run artifacts
    -> build deterministic explanation dossiers
    -> optionally generate LLM narratives
    -> launch planner UI / API
```

Do not couple an LLM call to optimization. A failed or absent LLM must not prevent a plan, numerical explanation, or what-if comparison.

## Current runnable data layer

The repository now implements the first five steps of that pipeline:

1. `download_data.py`
2. `ingest.py`
3. `validate_data.py`
4. `generate_demo_context.py`
5. `profile_data.py`

Operational expectations:

- The primary local database is `db/pricing_optimization.db`.
- Schema is applied automatically from `migrations/*.sql` and tracked in `schema_migrations`.
- Never edit an applied migration in place; add a new numbered migration.
- Observed public data and synthetic scenario data must remain distinguishable through explicit fields such as `origin`.

## Solver design direction

- Start with discrete allowed price points and a binary choose-one formulation.
- Objective: expected gross profit by default, with explicit optional revenue/volume/risk terms.
- Model hard constraints separately from soft constraints and their penalties.
- Give every generated constraint a stable rule ID and human-readable label.
- Support assumptions or equivalent diagnostics so infeasible runs can identify conflicting rules.
- Explain recommendations using controlled counterfactual re-solves: current-price lock, alternative-price lock, rule removal, and planner override.
- Do not call a solution "optimal" unless the persisted solver status and gap justify it; otherwise say "best found."

## Testing expectations

- Unit-test demand, revenue, margin, discount, and rounding formulas.
- Property-test choose-one, bound, ladder, budget, and lock constraints.
- Include known tiny portfolios whose optimum can be verified by exhaustive enumeration.
- Test infeasible rule sets and conflict diagnostics.
- Test scenario immutability and metric reconciliation.
- Test explanation grounding: every stated number and reason must map to a dossier field.
- Test LLM parsing with invalid, ambiguous, contradictory, and out-of-scope rules; no generated executable code.

## Local reference policy

- Reuse architectural lessons from `../xai_demand_forecasting`, especially staged execution, data-quality gates, exact model/run provenance, deterministic dossier builders, and optional narratives.
- Inspect `../dfs-ai-pricing` for domain concepts. Do not copy environment-specific Snowflake, Azure, Airflow, or portal code into the new core.
- The DFS implementation is a baseline to critique: surface silent override failures, non-optimal statuses, rule provenance, infeasibility, uncertainty, and portfolio opportunity cost explicitly.

## Documentation

- Keep `docs/PRD.md` aligned with accepted product decisions.
- Record material mathematical choices in `docs/decisions/` as short ADRs.
- Keep equations, units, sign conventions, and tolerances explicit.
