# PRD: Explainable Pricing Optimization

**Status:** Draft v0.2  
**Date:** 2026-06-28  
**Primary assumption to validate:** one upcoming pricing cycle, with product-level prices or discounts chosen simultaneously across a retail portfolio.

Implementation note: the repository now includes the v1 data-layer slice for this PRD: verified dunnhumby download, SQLite ingestion, profiling, validation, and deterministic synthetic pricing context generation.

## 1. Product thesis

Pricing teams do not only need a mathematically better number. They need a plan they can defend, adapt, and safely override.

The product converts a commercial objective, price-response evidence, and business rules into a reproducible optimization model. It returns a feasible portfolio plan plus the evidence required to answer:

1. What should we price each SKU at?
2. Why this price rather than the current or another allowed price?
3. Which rules mattered, and which are merely present?
4. What is the portfolio impact of a manual override?
5. If the request is impossible, which rules conflict and what relaxation would help?

This follows Alpha-Z's useful separation of **formulate -> solve -> explain**, while adding a planner workflow around rule authoring and controlled scenario comparison.

## 2. Target user and decision

### Primary user

A category/pricing manager planning the next retail pricing cycle for tens to hundreds of SKUs.

### Initial decision cadence

Single-period, batch planning: choose one price/discount for every in-scope SKU for the next week or campaign window. This is not real-time dynamic pricing.

### Recommended initial scenario

Tactical discount/price planning, rather than annual strategic base-price setting. The user's questions are expressed in discounts, and the DFS reference is also cycle-oriented. A later version can add multi-period base pricing and promotional calendars.

## 3. Goals

- Maximize expected portfolio gross profit under explicit business rules.
- Let a planner add common rules without editing optimization code.
- Produce faithful per-SKU and portfolio explanations from solver evidence.
- Support manual overrides as versioned child scenarios with immediate metric deltas.
- Diagnose infeasible rule combinations in business language.
- Demonstrate a realistic portfolio using public price/sales data plus transparent synthetic context.

## 4. Non-goals for MVP

- Training a demand forecast or elasticity model.
- Claiming causal price elasticity from the public dataset.
- Real-time or customer-level personalized pricing.
- Automatically publishing prices to production systems.
- Fully autonomous LLM decisions.
- Multi-period promotion scheduling, stock replenishment, or competitor reaction games.
- Exact modelling of every cross-SKU substitution effect in v1.

## 5. Why the DFS baseline is insufficient

The local `dfs-ai-pricing` implementation contains valuable domain primitives: allowed price-point generation, minimum-margin discount limits, inventory-aware conservative choices, category/style coupling, a global discount-spend constraint, fixed overrides, competitor price reporting, and what-if demand simulation.

The new system should address these gaps:

- Rule logic is embedded across scripts rather than represented as versioned, explainable objects.
- A fixed override can be ignored when no candidate matches, rather than creating a clear validation error or deliberate snapping policy.
- A non-optimal solve logs an error but can continue into output construction.
- There is no first-class infeasibility/conflict explanation.
- Competitor prices are mostly report context rather than configurable optimization rules.
- Explanations do not quantify the opportunity cost of constraints or overrides.
- Uncertainty and robustness are not explicit.
- The LLM/planner workflow for adding safe rules is absent.

## 6. Data plan

### Public dataset candidates

| Candidate | Strengths | Gaps | Recommendation |
|---|---|---|---|
| dunnhumby Breakfast at the Frat | 156 weeks; product/store/week units, spend, base and shelf price, promotion support, product and store metadata; explicitly intended for price sensitivity | no cost, inventory, or live competitor feed; historical and educational | Preferred for the pricing narrative and a manageable MVP subset |
| M5 Walmart | 3,049 products across 10 stores; daily units, weekly sell prices, calendar/events; already ingested by the sibling project | no cost, promotion mechanics, inventory, or competitors; price variation can be sparse | Best fallback for continuity and larger-scale testing |
| dunnhumby Complete Journey | rich transactions, products, campaigns, coupons, and causal promotion data | millions of rows and more preparation than the optimizer demo needs | Later validation dataset, not MVP |

### MVP input strategy

The optimizer will not estimate demand. It consumes a versioned `candidate_outcomes` table. Public data supplies realistic products, price ranges, hierarchy, current/reference prices, and demand scale. Missing commercial context is generated deterministically and marked `synthetic`.

Required logical inputs:

1. `products`: SKU, name, brand, category, pack/size, strategic role, current and reference price.
2. `candidate_outcomes`: SKU, candidate price, discount, expected units, revenue, gross profit, uncertainty band.
3. `commercial_context`: unit cost, inventory, competitor price/index, minimum margin, optional vendor funding.
4. `rules`: typed rule definitions with hard/soft priority, scope, parameters, source, author, and effective dates.
5. `scenario`: objective, planning period, input versions, rule-set version, overrides, and parent scenario.

Synthetic fields must use a committed seed and documented formulas. They must never be represented as observations.

## 7. Optimization model v1

### Decision variable

For every SKU `i` and allowed candidate `k`:

`x[i,k] = 1` if candidate price `k` is selected, otherwise `0`.

Exactly one candidate is selected per SKU.

### Objective

Default:

`maximize sum(expected_gross_profit[i,k] * x[i,k]) - soft_rule_penalties - risk_penalty`

Report revenue, units, gross margin, discount investment, and competitor price index even when they are not optimized.

### Initial hard-rule library

- candidate/price bounds and allowed price endings;
- minimum unit margin or gross-margin percentage;
- maximum price change versus current price;
- manual price/discount lock;
- competitor price-index floor/ceiling by SKU or category;
- price ladder or gap between related products/pack sizes;
- portfolio/category revenue or volume floor;
- maximum count of changed or discounted SKUs;
- inventory feasibility or minimum sell-through target;
- weighted average discount/trade-spend budget.

### Soft rules

Any eligible rule can be softened with an explicit unit penalty and reported violation. Defaults must not silently convert a hard rule to soft.

### Uncertainty

Each candidate should carry expected and conservative demand/profit values. MVP can support a risk-aversion weight or worst-case objective over a small scenario set. A plan must clearly say whether it is expected-value or risk-adjusted.

### Cross-SKU effects

MVP own-price response is sufficient for the first auditable solver. Category price ladders and portfolio constraints create meaningful coupling. Cross-price elasticity should follow as a deliberate extension using pairwise interaction terms or a nonlinear/iterative solve; it should not be faked by the LLM.

## 8. Explainability design

### Principle

The system explains a decision by recomputing controlled alternatives and inspecting model structure. It does not use generic feature attribution or free-form rationale generation.

### Per-SKU evidence dossier

- current, recommended, and locally best unconstrained price;
- expected units, revenue, gross profit, margin, and uncertainty at each;
- selected candidate rank among feasible local candidates;
- rules that excluded alternatives;
- portfolio constraints that make the local choice globally useful;
- objective loss if forced to current price or a planner-selected alternative;
- downstream changes to other SKUs after a forced-price re-solve;
- solver status, optimality gap, and evidence version.

### Explanation types

- **Why this price?** Contrast recommended price with current price and the strongest feasible alternative.
- **Why not a deeper discount?** Identify violated rule(s) or quantified portfolio profit loss.
- **What constrained this SKU?** Separate binding/decisive rules from non-binding rules.
- **What if I override it?** Re-solve a child scenario and show portfolio deltas and displaced recommendations.
- **Why no plan?** Return conflicting hard rules and candidate relaxations; do not fabricate a near-feasible plan.

### LLM responsibilities

- Convert a planner utterance into a proposed typed rule JSON from an allowlisted schema.
- Ask for clarification when scope, metric, unit, period, or hard/soft intent is ambiguous.
- Render deterministic evidence dossiers in concise business language.
- Cite dossier fields in the response payload so every claim is traceable.

The UI must show the parsed rule and estimated impact before activation. The LLM cannot emit executable solver code.

## 9. Key workflows and acceptance criteria

### A. Optimize a base scenario

The user selects a portfolio, objective, and rule set and runs optimization.

Acceptance:

- every in-scope SKU has one recommendation or the run fails explicitly;
- plan metrics reconcile to SKU rows;
- hard-rule audit has zero violations;
- status distinguishes optimal, feasible/best-found, infeasible, and error;
- run artifacts are immutable and reproducible.

### B. Add a custom rule in natural language

Example: "Keep all premium cereal within 5% above Brand X and do not let margin fall below 25%."

Acceptance:

- LLM returns typed, scoped rule proposals using known product/category IDs and units;
- invalid fields or unsupported semantics cannot reach the solver;
- the user sees interpretation, affected SKU count, and feasibility preview;
- activation requires confirmation and creates a new rule-set version.

### C. Ask why a discount was chosen

Acceptance:

- response contrasts at least one relevant alternative;
- all numbers come from persisted candidate or re-solve artifacts;
- response names decisive rules only when evidence supports them;
- uncertainty and non-optimal solver status are visible.

### D. Override a price

Acceptance:

- exact unsupported prices produce a validation choice: reject or snap with explicit consent;
- override creates a child scenario and re-solves the full portfolio;
- UI shows SKU and portfolio deltas for profit, revenue, units, margin, and rule feasibility;
- original scenario remains unchanged.

## 10. Proposed product surfaces

1. **Plan overview:** portfolio KPIs, recommendations, warnings, constraint utilization, solver status.
2. **SKU decision inspector:** demand/profit curve, recommended/current/alternative points, evidence-backed explanation.
3. **Rule studio:** structured rule editor plus natural-language proposal box, validation, impact preview, version history.
4. **Scenario compare:** base versus override/rule-change metrics, changed SKUs, and reasons.
5. **Run audit:** input hashes, rule IDs, solver configuration, status/gap, timing, and downloadable evidence.

## 11. Success measures

### Correctness

- zero unexplained hard-rule violations;
- 100% financial reconciliation within tolerance;
- tiny benchmark portfolios match exhaustive enumeration;
- 100% of narrative numerical claims trace to evidence fields.

### Decision usefulness

- planner can add a supported rule and understand its effect without code;
- planner can explain a selected SKU price using a quantified alternative;
- override impact is available from one child-scenario re-solve;
- infeasible requests return actionable conflicts rather than a generic solver error.

Performance targets should be set after confirming portfolio size and deployment surface.

## 12. Delivery slices

1. **Foundation:** dataset profile, deterministic scenario generator, schemas, formula tests.
2. **Solver:** candidate generation contract, objective, core constraints, exhaustive-oracle tests, persisted runs.
3. **Deterministic XAI:** alternative locks, rule evidence, scenario diff, infeasibility diagnostics.
4. **Planner UI/API:** overview, SKU inspector, rule studio, override compare.
5. **LLM layer:** typed rule proposals and grounded narratives, optional and independently re-runnable.
6. **Hardening:** uncertainty scenarios, larger portfolio, cross-price effects, performance and governance.

## 13. Open product decisions

1. Is the first scenario **temporary promotion/discount planning**, **permanent base-price optimization**, or a generic demo supporting both?
2. Is the intended user a **retailer category manager** (optimizes shelf margin) or a **brand/CPG revenue manager** (may optimize manufacturer and retailer economics separately)?
3. What should the default objective be: **gross profit**, revenue subject to margin, sell-through, or a weighted score?
4. Should v1 optimize one period only, or must it choose prices over several weeks with cooldown/promotion-spacing rules?
5. What demo scale matters: roughly 20, 100, 1,000, or 3,000 SKUs?
6. Should the prototype be a Streamlit decision app like the sibling project, an API-first service, or both?

## 14. References

- Alpha-Z, [AI for decision-making / Pricing & revenue](https://www.alpha-z.io/applications/pricing).
- dunnhumby, [Source Files](https://www.dunnhumby.com/source-files/).
- Llenas et al. (2026), [PepsiCo Deploys AI-Driven Pricing and Promotion Optimization at Scale](https://arxiv.org/abs/2606.17941).
- Cohen et al., [A retail store SKU promotions optimization model for category multi-period profit maximization](https://eprints.lancs.ac.uk/id/eprint/83834/).
