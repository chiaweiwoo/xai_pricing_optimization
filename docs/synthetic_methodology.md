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

- Strategic role: assigned from recent revenue rank so the portfolio includes KVIs, traffic drivers, margin drivers, and long-tail items.
- Elasticity: category and role dependent. Cereal is made more price sensitive; some pizza and margin-driver items are intentionally more inelastic.
- Unit cost: derived as a role-dependent share of current price to create meaningful margin trade-offs.
- Inventory: weeks of cover are based on recent demand scale, with a few deliberate tension cases such as overstocked pretzels and scarce frozen pizza.
- Competitor price: set around current price with category-specific pressure so the scenario produces non-trivial recommendation differences.
- Candidate outcomes: produced by a constant-elasticity model over discrete price candidates, then capped by available inventory.

## Guardrails

- Every generated record is labeled `origin = 'synthetic'`.
- The synthetic generator is seed-stable.
- The demand model is explicitly a placeholder object for the later solver, not a fitted causal model.
