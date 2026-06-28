from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sys

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from xai_pricing.config import DB_PATH, DEFAULT_SCENARIO_ID
from xai_pricing.conversation import PricingConversationService
from xai_pricing.db import get_conn
from xai_pricing.planner import PlanBundle, PricingDecisionService
from xai_pricing.ui_support import build_roi_snapshot, build_selection_snapshot, select_review_cases


st.set_page_config(page_title="XAI Pricing Optimization", page_icon=":material/local_offer:", layout="centered")

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');

    :root {
        --bg-top: #f5ecdf;
        --bg-bottom: #fffaf4;
        --panel: rgba(255, 252, 247, 0.94);
        --panel-soft: rgba(255, 246, 235, 0.88);
        --ink: #1f1d1a;
        --muted: #6f655a;
        --accent: #bf5a2f;
        --accent-soft: rgba(191, 90, 47, 0.10);
        --good-soft: rgba(78, 126, 96, 0.12);
        --border: rgba(109, 86, 60, 0.14);
    }

    html, body, [class*="css"] {
        font-family: 'IBM Plex Sans', sans-serif;
        color: var(--ink);
    }

    .stApp {
        background:
            radial-gradient(circle at top left, rgba(191, 90, 47, 0.12), transparent 30%),
            radial-gradient(circle at top right, rgba(78, 126, 96, 0.10), transparent 22%),
            linear-gradient(180deg, var(--bg-top), var(--bg-bottom));
    }

    .block-container {
        max-width: 960px;
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }

    .hero, .panel {
        background: linear-gradient(135deg, rgba(255,255,255,0.96), rgba(255,244,232,0.90));
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: 0 18px 48px rgba(103, 76, 52, 0.08);
    }

    .hero {
        padding: 1.5rem 1.6rem;
        margin-bottom: 1rem;
    }

    .panel {
        padding: 1rem 1.1rem;
        margin-bottom: 0.9rem;
    }

    .eyebrow {
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--accent);
        font-size: 0.76rem;
        font-weight: 700;
    }

    .hero h1 {
        font-size: 2.15rem;
        line-height: 1.05;
        margin: 0.2rem 0 0.4rem 0;
    }

    .hero p, .subtle {
        color: var(--muted);
        margin: 0;
    }

    .pill-row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin-top: 0.85rem;
    }

    .pill {
        border-radius: 999px;
        border: 1px solid var(--border);
        background: var(--panel-soft);
        padding: 0.3rem 0.7rem;
        font-size: 0.8rem;
        font-weight: 600;
    }

    .kpi {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 20px;
        padding: 1rem;
        min-height: 132px;
        box-shadow: 0 10px 26px rgba(103, 76, 52, 0.05);
    }

    .kpi-label {
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.06em;
        font-size: 0.78rem;
        margin-bottom: 0.45rem;
    }

    .kpi-value {
        font-size: 1.65rem;
        font-weight: 700;
        line-height: 1.05;
        margin-bottom: 0.3rem;
    }

    .kpi-copy {
        color: var(--muted);
        font-size: 0.92rem;
    }

    div[data-testid="stDataFrame"], div[data-testid="stExpander"] {
        background: rgba(255, 252, 247, 0.92);
        border-radius: 18px;
        border: 1px solid var(--border);
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _currency(value: float | int) -> str:
    return f"${float(value):,.2f}"


def _pct(value: float | int) -> str:
    return f"{float(value):.1%}"


def _count(value: float | int) -> str:
    return f"{int(round(float(value))):,}"


def _decimal(value: float | int) -> str:
    return f"{float(value):,.4f}"


def _multiple(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.2f}x"


def _campaign_id(conn) -> str | None:
    rows = conn.execute(
        """
        SELECT scenario_id
        FROM scenarios
        ORDER BY CASE WHEN scenario_id = ? THEN 0 ELSE 1 END, created_at DESC, scenario_id
        """,
        (DEFAULT_SCENARIO_ID,),
    ).fetchall()
    if not rows:
        return None
    return str(rows[0]["scenario_id"])


def _kpi_card(label: str, value: str, copy: str) -> None:
    st.markdown(
        f"""
        <div class="kpi">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value">{value}</div>
            <div class="kpi-copy">{copy}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _recommendation_table(plan: PlanBundle) -> pd.DataFrame:
    frame = pd.DataFrame(plan.official.selections)
    catalog = {row["upc"]: row for row in plan.catalog}
    current = plan.current_price.summary
    records: list[dict[str, object]] = []
    for row in frame.to_dict("records"):
        product = catalog.get(str(row["upc"]), {})
        expected_units = float(row.get("expected_units", 0.0) or 0.0)
        on_hand_units = float(product.get("on_hand_units", 0.0) or 0.0)
        records.append(
            {
                "_role_sort": str(row.get("role", "")),
                "_discount_sort": float(row.get("discount_pct", 0.0) or 0.0),
                "_gp_sort": float(row.get("gross_profit", 0.0) or 0.0),
                "SKU": row["upc"],
                "Product": row.get("product_label", row["upc"]),
                "Role": str(row.get("role", "")).replace("_", " ").title(),
                "Discount": _pct(row.get("discount_pct", 0.0)),
                "Price": _currency(row.get("candidate_price", 0.0)),
                "Expected demand": _count(expected_units),
                "On hand": _count(on_hand_units),
                "Expected ending stock": _count(row.get("ending_inventory_units", 0.0)),
                "Upside stockout risk": "Yes" if float(row.get("optimistic_lost_units", 0.0) or 0.0) > 0 else "No",
                "Expected gross profit": _currency(row.get("gross_profit", 0.0)),
                "Competitor gap (0 = within tolerance)": _decimal(row.get("competitor_gap", 0.0)),
            }
        )
    result = pd.DataFrame(records)
    if result.empty:
        return result
    result = result.sort_values(["_role_sort", "_discount_sort", "_gp_sort"], ascending=[True, False, False])
    return result.drop(columns=["_role_sort", "_discount_sort", "_gp_sort"])


def _benchmark_table(plan: PlanBundle) -> pd.DataFrame:
    rows = [
        ("Official recommendation", plan.official.summary, "Fixed reference proposal"),
        ("Current-price baseline", plan.current_price.summary, "Keep every SKU at current price"),
        ("Price-position-first benchmark", plan.position_first.summary, "Prioritize competitor alignment before profit"),
        ("Theoretical profit ceiling", plan.theoretical_ceiling.summary, "Best SKU-level profit points without the 10% portfolio budget"),
    ]
    return pd.DataFrame(
        [
            {
                "View": label,
                "Expected gross profit": _currency(summary["total_gross_profit"]),
                "Revenue": _currency(summary["total_revenue"]),
                "Markdown rate used": _pct(summary["budget_utilization_pct"]),
                "Promoted SKUs": _count(summary["promoted_products"]),
                "Weighted competitor gap": _decimal(summary["weighted_competitor_gap"]),
                "Why it exists": explainer,
            }
            for label, summary, explainer in rows
        ]
    )


def _focus_cards(plan: PlanBundle, planner: PricingDecisionService) -> None:
    current_rows = []
    for row in plan.official.selections:
        dossier = planner.get_sku_dossier(plan.official.run_id, row["upc"])
        current_rows.append(
            {
                "upc": row["upc"],
                "discount_pct": dossier["current"]["discount_pct"],
                "gross_profit": dossier["current"]["gross_profit"],
                "competitor_gap": dossier["current"]["competitor_gap"],
            }
        )
    snapshot = build_selection_snapshot(plan.official.selections, current_rows, plan.catalog)
    cases = select_review_cases(snapshot)
    if not cases:
        return

    st.subheader("Three useful products to inspect")
    st.caption("These are picked automatically to make the campaign story easier to read.")
    cols = st.columns(len(cases))
    for idx, case in enumerate(cases):
        dossier = planner.get_sku_dossier(plan.official.run_id, case["upc"])
        with cols[idx]:
            st.markdown(f"**{case['title']}**")
            st.caption(case["description"])
            st.write(dossier["product"]["product_label"])
            st.write(
                f"Official discount {_pct(dossier['selected']['discount_pct'])} | "
                f"Expected ending stock {_count(dossier['selected']['ending_inventory_units'])}"
            )


def _render_assistant(plan: PlanBundle, conversation: PricingConversationService) -> None:
    st.subheader("Ask the assistant")
    st.caption(
        "Use one message box for everything. The assistant will classify your intent automatically, stay inside a small supported scope, and keep the official proposal unchanged."
    )
    st.info(
        "Supported questions: summarize the campaign, explain one SKU's discount, ask why another discount was not chosen, force one allowed discount as a what-if, test a different budget, or test a minimum-margin rule for one SKU."
    )

    starters = [
        "Summarize the proposal",
        "Why is this SKU at 10%?",
        "Why not 15% for this SKU?",
        "What if we force 5% for this SKU?",
        "What if budget becomes 8%?",
    ]
    st.caption("Example prompts")
    st.write(" | ".join(starters))

    chat_key = f"chat_history_{plan.scenario_id}"
    if chat_key not in st.session_state:
        st.session_state[chat_key] = []

    for item in st.session_state[chat_key]:
        with st.chat_message(item["role"]):
            st.markdown(item["content"])

    prompt = st.chat_input("Ask about the proposal or try a bounded what-if")
    if not prompt:
        return

    st.session_state[chat_key].append({"role": "user", "content": prompt})
    turn = conversation.handle_question(plan, prompt)
    st.session_state[chat_key].append({"role": "assistant", "content": turn.response_text})

    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        st.markdown(turn.response_text)


def _product_explainer(plan: PlanBundle, planner: PricingDecisionService) -> None:
    st.subheader("Inspect one product")
    catalog = {row["upc"]: row for row in plan.catalog}
    labels = {
        row["upc"]: f"{row.get('product_label', row['upc'])} [{row['upc']}]"
        for row in plan.official.selections
    }
    selected_upc = st.selectbox("Choose a SKU", options=list(labels.keys()), format_func=lambda value: labels[value])
    dossier = planner.get_sku_dossier(plan.official.run_id, selected_upc)

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Current vs official**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "View": "Current",
                        "Discount": _pct(dossier["current"]["discount_pct"]),
                        "Price": _currency(dossier["current"]["candidate_price"]),
                        "Expected demand": _count(dossier["current"]["expected_units"]),
                        "Expected ending stock": _count(dossier["current"]["ending_inventory_units"]),
                        "Gross profit": _currency(dossier["current"]["gross_profit"]),
                    },
                    {
                        "View": "Official",
                        "Discount": _pct(dossier["selected"]["discount_pct"]),
                        "Price": _currency(dossier["selected"]["candidate_price"]),
                        "Expected demand": _count(dossier["selected"]["expected_units"]),
                        "Expected ending stock": _count(dossier["selected"]["ending_inventory_units"]),
                        "Gross profit": _currency(dossier["selected"]["gross_profit"]),
                    },
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    with col2:
        st.markdown("**Context**")
        st.write(f"On hand: {_count(catalog[selected_upc].get('on_hand_units', 0))}")
        st.write(f"Competitor price: {_currency(catalog[selected_upc].get('competitor_price', 0) or 0)}")
        st.write(f"Minimum margin: {_pct(catalog[selected_upc].get('min_margin_pct', 0))}")
        st.caption("Inbound inventory is shown in the dataset for business context, but the current one-period solve protects only against expected demand versus current on-hand stock.")

    rows = []
    for alt in dossier["alternatives"]:
        rows.append(
            {
                "Discount": _pct(alt["discount_pct"]),
                "Price": _currency(alt["candidate_price"]),
                "Expected demand": _count(alt["expected_units"]),
                "Expected ending stock": _count(alt["ending_inventory_units"]),
                "Gross margin": _pct(alt["gross_margin_pct"]),
                "Gross profit": _currency(alt["gross_profit"]),
                "Valid": "Yes" if alt["effective_hard_valid"] else "No",
                "Blocked by": alt["reason"] or "",
                "Selected": "Yes" if alt["is_selected"] else "",
            }
        )
    st.markdown("**Allowed discount menu for this product**")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


@st.dialog("How the pricing solver works", width="large")
def _show_model_overlay(plan: PlanBundle) -> None:
    roi_snapshot = build_roi_snapshot(plan.official.summary, plan.current_price.summary)
    objective_order = plan.brief.get("objective_order", [])

    st.markdown("**Business decision**")
    st.write("Choose exactly one allowed discount from 0%, 5%, 10%, 15%, 20%, or 25% for every SKU in the campaign.")

    st.markdown("**Decision variable**")
    st.latex(r"x_{i,d} \in \{0,1\}")
    st.caption("`x_(i,d) = 1` means SKU `i` is assigned discount `d`.")

    st.markdown("**Core calculations for one SKU and one allowed discount**")
    st.latex(r"p_{i,d} = \text{list\_price}_i \times (1-d)")
    st.latex(r"q_{i,d} = \text{baseline\_units}_i \times \left(\frac{p_{i,d}}{\text{reference\_price}_i}\right)^{\text{elasticity}_i}")
    st.latex(r"q^{cap}_{i,d} = \min(q_{i,d}, \text{on\_hand}_i)")
    st.latex(r"GP_{i,d} = q^{cap}_{i,d} \times (p_{i,d} - \text{unit\_cost}_i)")
    st.latex(r"MD_{i,d} = q^{cap}_{i,d} \times (\text{list\_price}_i - p_{i,d})")
    st.caption(
        "Gross profit already reflects the discount because the selling price inside the formula is the discounted price."
    )

    st.markdown("**Objective**")
    st.write("This is a maximization-first model with tie-breakers solved in sequence.")
    if objective_order:
        for idx, label in enumerate(objective_order, start=1):
            st.write(f"{idx}. {label}")

    st.markdown("**Hard constraints**")
    st.latex(r"\sum_d x_{i,d} = 1 \quad \forall i")
    st.caption("Exactly one allowed discount per SKU.")
    st.latex(r"GP\text{ margin}_{i,d} \ge \text{minimum margin}_i")
    st.caption("Candidates below the SKU's margin floor are removed before solving.")
    st.latex(r"q_{i,d} \le \text{on\_hand}_i")
    st.caption("Expected demand cannot exceed current on-hand inventory, so expected ending stock stays non-negative.")
    st.latex(r"\frac{\sum_{i,d} MD_{i,d} x_{i,d}}{\sum_{i,d} \text{list\_price}_i \times q^{cap}_{i,d} x_{i,d}} \le 10\%")
    st.caption("The campaign-level markdown rate cannot exceed the 10% budget policy.")

    st.markdown("**Soft preferences and diagnostics**")
    tradeoff_pct = float(plan.brief.get("competitor_tradeoff_tolerance_pct", 0.0) or 0.0)
    st.write("Competitor price position is relevant, but it is not a hard block in the official proposal.")
    st.latex(r"\text{gap}_{i,d} = \max \left(0, \frac{p_{i,d}}{\text{competitor\_price}_i} - (1 + \text{tolerance}_i)\right)")
    st.caption(
        f"After finding the best gross-profit level, the official solve may give up at most {_pct(tradeoff_pct)} of gross profit to reduce weighted competitor gap, then it prefers shallower discount depth."
        if tradeoff_pct > 0
        else "If two profit-equivalent portfolios exist, the solver prefers the one with the smaller weighted competitor gap, then the shallower overall discount depth."
    )
    st.write("Upside stockout risk is a diagnostic only. It highlights SKUs that could still stock out in an optimistic demand case, but it does not make the official plan infeasible.")

    st.markdown("**ROI for this briefing**")
    st.write("For business reading, the clean comparison is against the current-price baseline.")
    st.latex(r"\Delta GP = GP_{official} - GP_{current}")
    st.latex(r"\text{return on markdown spend} = \frac{\Delta GP}{\text{official markdown spend}}")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Metric": "Official expected gross profit",
                    "Value": _currency(roi_snapshot["official_gross_profit"] or 0.0),
                    "Why it matters": "Solver-selected portfolio result",
                },
                {
                    "Metric": "Current-price expected gross profit",
                    "Value": _currency(roi_snapshot["current_gross_profit"] or 0.0),
                    "Why it matters": "Reference point if we made no promotional changes",
                },
                {
                    "Metric": "Incremental gross profit",
                    "Value": _currency(roi_snapshot["incremental_gross_profit"] or 0.0),
                    "Why it matters": "Gross-profit lift versus current pricing",
                },
                {
                    "Metric": "Official markdown spend",
                    "Value": _currency(roi_snapshot["markdown_spend"] or 0.0),
                    "Why it matters": "Discount investment used by the official plan",
                },
                {
                    "Metric": "Return on markdown spend",
                    "Value": _multiple(roi_snapshot["return_on_markdown"]),
                    "Why it matters": "Incremental gross profit returned per $1 of markdown spend",
                },
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.info(
        "We do not define ROI as `delta profit - budget`. That mixes a dollar delta with a budget cap and would also double-count the discount effect already included in gross profit."
    )


def main() -> None:
    if not DB_PATH.exists():
        st.error("Database not found. Run `uv run python ingest.py` and `uv run python generate_demo_context.py` first.")
        return

    with closing(get_conn()) as conn:
        scenario_id = _campaign_id(conn)
        if not scenario_id:
            st.error("No pricing campaign found. Run `uv run python generate_demo_context.py` first.")
            return

        planner = PricingDecisionService(conn)
        conversation = PricingConversationService(planner)
        plan = planner.build_plan_bundle(scenario_id)

        st.markdown(
            f"""
            <div class="hero">
                <div class="eyebrow">Pricing Optimization Briefing</div>
                <h1>One campaign, all SKUs, one official recommendation.</h1>
                <p>This demo assumes a demand-response model already exists for each SKU. The solver chooses one allowed discount from 0%, 5%, 10%, 15%, 20%, or 25% for every product while respecting a 10% markdown budget, minimum margin rules, and on-hand inventory.</p>
                <div class="pill-row">
                    <div class="pill">Campaign: {plan.scenario.get('scenario_name', scenario_id)}</div>
                    <div class="pill">Planning week: {plan.scenario.get('planning_week_end', 'n/a')}</div>
                    <div class="pill">Budget limit: {_pct(plan.scenario.get('budget_pct', 0.10))}</div>
                    <div class="pill">Official run: {plan.official.run_id}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.subheader("What you are looking at")
        explain_col, button_col = st.columns([0.78, 0.22], vertical_alignment="bottom")
        with button_col:
            if st.button("View mathematical model", width="stretch"):
                _show_model_overlay(plan)
        st.caption(
            "This page is a pricing recommendation briefing, not a demand-forecasting build. Some inputs come from the public dunnhumby dataset, while competitor price, inventory, cost, and elasticity parameters are synthetic so the solver has enough business context to tell a realistic pricing story."
        )

        kpi_cols = st.columns(4)
        with kpi_cols[0]:
            _kpi_card("Expected gross profit", _currency(plan.official.summary["total_gross_profit"]), "Official recommendation")
        with kpi_cols[1]:
            _kpi_card("Budget used", _pct(plan.official.summary["budget_utilization_pct"]), f"Limit {_pct(plan.official.summary['budget_limit_pct'])}")
        with kpi_cols[2]:
            _kpi_card("Promoted SKUs", _count(plan.official.summary["promoted_products"]), f"Protected at 0%: {_count(plan.official.summary['protected_products'])}")
        with kpi_cols[3]:
            _kpi_card("Upside stockout risk", _count(plan.official.summary.get("upside_risk_products", 0)), "Risk only in upside demand case")

        st.subheader("Official recommendation")
        st.caption("Every row below belongs to the same campaign. Inventory feasibility is based on expected demand versus current on-hand stock.")
        st.dataframe(_recommendation_table(plan), hide_index=True, width="stretch")

        with st.expander("How to read the data", expanded=False):
            st.caption("This is the minimum context needed to interpret the proposal.")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Field group": "Observed data", "Meaning": "Products, historical prices, historical units, and baseline retail behavior from dunnhumby Breakfast at the Frat."},
                        {"Field group": "Synthetic business context", "Meaning": "On-hand inventory, unit cost, competitor price, strategic role, and elasticity settings created for this pricing-optimization demo."},
                        {"Field group": "Candidate outcomes", "Meaning": "For each SKU and allowed discount, we project demand, gross profit, ending inventory, and competitor gap. A competitor gap of 0 means the price is already within the allowed tolerance versus the competitor."},
                    ]
                ),
                hide_index=True,
                width="stretch",
            )

        with st.expander("Benchmark views", expanded=False):
            st.caption("These are comparison views only. The official recommendation stays fixed unless you explicitly rerun a separate what-if.")
            st.dataframe(_benchmark_table(plan), hide_index=True, width="stretch")

        _focus_cards(plan, planner)
        _product_explainer(plan, planner)
        _render_assistant(plan, conversation)


if __name__ == "__main__":
    main()
