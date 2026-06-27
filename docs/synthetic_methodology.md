# Synthetic Context Methodology

The public dataset is rich enough to anchor a realistic pricing story, but not enough to solve a real optimization problem by itself. The missing fields are commercial, not analytical.

## Why we synthesize

The optimizer needs:

- unit cost
- competitor price
- inventory
- an upstream demand model

Those do not exist in the public workbook, so we generate them deterministically from a fixed seed and observed product behavior.

## How fields are generated

- Anchor price: each SKU uses one trailing non-promo list/reference price. The current price is initialized to the same value so promotion planning starts from a clean regular-price baseline.
- Strategic role: assigned from recent revenue rank so the portfolio includes KVIs, traffic drivers, margin drivers, and long-tail items.
- Archetype: each SKU is mapped into a deterministic commercial story such as `competitor_pressure`, `low_inventory`, `overstock`, `margin_constrained`, `promotion_opportunity`, or `neutral`.
- Elasticity: archetype and category dependent. Cereal and promo-oriented items are more price sensitive; some pizza and margin-driver items are intentionally less elastic.
- Unit cost: derived as a share of list price to create meaningful margin trade-offs while preserving at least one safe option per SKU.
- Inventory: weeks of cover are based on recent demand scale, then scaled by profile. The `inventory_stress_v1` profile deliberately reduces cover and inbound flow.
- Competitor price: generated from list price with deeper pressure for selected overstock and promotion-opportunity items so the solver must make real trade-offs.
- Candidate outcomes: produced by a constant-elasticity model over the discrete menu `{0%, 5%, 10%, 15%, 20%, 25%}` and annotated with markdown spend, ending inventory, lost demand, competitor gap, and hard-rule validity.

## Guardrails

- Every generated record is labeled `origin = 'synthetic'`.
- The synthetic generator is seed-stable.
- The demand model is explicitly a placeholder object for the later solver, not a fitted causal model.
- The generator repairs a SKU if the first pass leaves it with no hard-valid candidate. The repair reduces cost pressure and/or adds enough inventory so the baseline option remains feasible.
- Two official profiles are maintained: `balanced_campaign_v1` and `inventory_stress_v1`.
