from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sys

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from xai_pricing.config import DB_PATH
from xai_pricing.conversation import PricingConversationService
from xai_pricing.db import get_conn
from xai_pricing.planner import PricingDecisionService


SCENARIO_GUIDE = {
    "balanced_campaign_v1": "A balanced promotional scenario. Inventory is healthy enough to allow a mix of protected SKUs and promoted SKUs, while competitor pressure still matters.",
    "inventory_stress_v1": "A tighter inventory scenario. The optimizer protects more SKUs because deeper discounts can push projected ending inventory below the safety-stock target.",
    "demo_pricing_v1": "A demo scenario generated from the same synthetic logic. Treat it as a sandbox version of the balanced setup.",
}

ROLE_GUIDE = {
    "kvi": "Key value item. A visible item where relative price matters for shopper perception.",
    "traffic_driver": "An item used to attract trips or baskets, so sharper discounts can be acceptable.",
    "margin_driver": "An item where the business wants to protect profit rate more carefully.",
    "long_tail": "A lower-priority item with less strategic weight than the headline products.",
}

ARCHETYPE_GUIDE = {
    "competitor_pressure": "Competitor is priced aggressively, so the model pays more attention to price position.",
    "low_inventory": "Inventory is relatively tight, so large discounts are risky.",
    "overstock": "There is room to push more units, so deeper discounts can make sense.",
    "margin_constrained": "Cost structure limits how far the price can be reduced safely.",
    "promotion_opportunity": "Demand is more responsive, so moderate discounts can unlock attractive volume.",
    "neutral": "No special synthetic stressor dominates the decision.",
}

GLOSSARY_ROWS = [
    {"term": "Official proposal", "meaning": "The main recommended plan. This is the fixed reference plan used for explanation and comparison."},
    {"term": "Profit-first feasible", "meaning": "A comparison plan that respects hard rules and budget but maximizes gross profit before competitor position."},
    {"term": "Current-price baseline", "meaning": "The outcome if we keep the current price point for every SKU."},
    {"term": "Theoretical ceiling", "meaning": "The best gross-profit point for each SKU independently. It is useful as an upper bound, not as the final proposal."},
    {"term": "Weighted competitor gap", "meaning": "How far we price above the tolerated competitor position, weighted by strategic importance. Lower is better."},
    {"term": "Budget utilization", "meaning": "Markdown spend as a share of list-price revenue. The scenario limit is typically 10%."},
    {"term": "Local best feasible", "meaning": "The best gross-profit candidate for one SKU after hard rules are applied, before portfolio trade-offs are considered."},
    {"term": "Safety stock", "meaning": "Inventory buffer the solver tries to protect so a promotion does not create a stockout risk."},
]

BENCHMARK_GUIDE = [
    {"plan": "Official proposal", "how_to_read": "Main answer. Optimizes competitor position first, then gross profit, then avoids unnecessary discount depth."},
    {"plan": "Profit-first feasible", "how_to_read": "Shows what we could earn if competitor positioning were not the first priority."},
    {"plan": "Current-price baseline", "how_to_read": "Useful anchor for asking whether the recommended campaign improves the current state."},
    {"plan": "Theoretical ceiling", "how_to_read": "Best independent SKU points. Good for showing remaining headroom, but not a portfolio recommendation."},
]

DATA_PROVENANCE_ROWS = [
    {"field_group": "Public data", "details": "Products, store metadata, historical weekly units, spend, shelf price, base price, and promo flags from dunnhumby Breakfast at the Frat."},
    {"field_group": "Synthetic but deterministic", "details": "Unit cost, competitor price, inventory, strategic role, archetype, elasticity parameters, and price-point outcomes. These are generated from rules, not observed in the workbook."},
    {"field_group": "Optimization input", "details": "A discrete menu of price/discount candidates: 0%, 5%, 10%, 15%, 20%, and 25%, each with projected units, revenue, gross profit, and inventory impact."},
]

PHASE_GUIDE = {
    "competitor_gap": "Minimize weighted competitor gap. This protects price perception on strategically important items.",
    "gross_profit": "Maximize gross profit after honoring the earlier phase result.",
    "discount_depth": "Prefer shallower discounts when the earlier phases are tied closely enough.",
}


st.set_page_config(
    page_title="XAI Pricing Optimization",
    page_icon=":material/local_offer:",
    layout="wide",
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

    :root {
        --bg-top: #f9efe3;
        --bg-bottom: #fffaf4;
        --surface: rgba(255, 252, 247, 0.85);
        --surface-strong: rgba(255, 247, 236, 0.96);
        --ink: #1f1d1a;
        --muted: #6a6258;
        --accent: #d96c3c;
        --accent-soft: rgba(217, 108, 60, 0.12);
        --border: rgba(103, 76, 52, 0.14);
    }

    html, body, [class*="css"]  {
        font-family: 'IBM Plex Sans', sans-serif;
        color: var(--ink);
    }

    .stApp {
        background:
            radial-gradient(circle at top left, rgba(217, 108, 60, 0.12), transparent 34%),
            radial-gradient(circle at top right, rgba(68, 124, 93, 0.08), transparent 28%),
            linear-gradient(180deg, var(--bg-top), var(--bg-bottom));
    }

    .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
    }

    .hero {
        background: linear-gradient(135deg, rgba(255,255,255,0.92), rgba(255,243,231,0.82));
        border: 1px solid var(--border);
        border-radius: 24px;
        padding: 1.4rem 1.6rem;
        box-shadow: 0 18px 48px rgba(103, 76, 52, 0.08);
        margin-bottom: 1rem;
    }

    .eyebrow {
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--accent);
        font-size: 0.78rem;
        font-weight: 700;
    }

    .hero h1 {
        font-size: 2rem;
        margin: 0.15rem 0 0.25rem 0;
    }

    .hero p {
        color: var(--muted);
        margin: 0;
        max-width: 72rem;
    }

    div[data-testid="stMetric"] {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 0.65rem 0.85rem;
        box-shadow: 0 10px 28px rgba(103, 76, 52, 0.05);
    }

    div[data-testid="stDataFrame"], div[data-testid="stExpander"] {
        background: var(--surface-strong);
        border-radius: 18px;
        border: 1px solid var(--border);
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _format_pct(value: float) -> str:
    return f"{value:.1%}"


def _format_currency(value: float) -> str:
    return f"{value:,.2f}"


def _load_scenarios(conn) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT scenario_id, scenario_name, profile_id, budget_pct, safety_stock_pct
        FROM scenarios
        ORDER BY created_at DESC, scenario_id
        """,
        conn,
    )


def _bundle_to_benchmark_table(bundle) -> pd.DataFrame:
    rows = [
        {
            "plan": "Official proposal",
            "status": bundle.official.status,
            "gross_profit": bundle.official.summary["total_gross_profit"],
            "revenue": bundle.official.summary["total_revenue"],
            "budget_pct": bundle.official.summary["budget_utilization_pct"],
            "competitor_gap": bundle.official.summary["weighted_competitor_gap"],
        },
        {
            "plan": "Profit-first feasible",
            "status": bundle.profit_first.status,
            "gross_profit": bundle.profit_first.summary["total_gross_profit"],
            "revenue": bundle.profit_first.summary["total_revenue"],
            "budget_pct": bundle.profit_first.summary["budget_utilization_pct"],
            "competitor_gap": bundle.profit_first.summary["weighted_competitor_gap"],
        },
        {
            "plan": "Current-price baseline",
            "status": bundle.current_price.status,
            "gross_profit": bundle.current_price.summary["total_gross_profit"],
            "revenue": bundle.current_price.summary["total_revenue"],
            "budget_pct": bundle.current_price.summary["budget_utilization_pct"],
            "competitor_gap": bundle.current_price.summary["weighted_competitor_gap"],
        },
        {
            "plan": "Theoretical ceiling",
            "status": bundle.theoretical_ceiling.status,
            "gross_profit": bundle.theoretical_ceiling.summary["total_gross_profit"],
            "revenue": bundle.theoretical_ceiling.summary["total_revenue"],
            "budget_pct": bundle.theoretical_ceiling.summary["budget_utilization_pct"],
            "competitor_gap": bundle.theoretical_ceiling.summary["weighted_competitor_gap"],
        },
    ]
    frame = pd.DataFrame(rows)
    frame = frame.rename(
        columns={
            "plan": "Plan",
            "status": "Status",
            "gross_profit": "Gross Profit",
            "revenue": "Revenue",
            "budget_pct": "Budget Used",
            "competitor_gap": "Weighted Competitor Gap",
        }
    )
    frame["Budget Used"] = frame["Budget Used"].map(_format_pct)
    return frame


def _phase_table(run) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "phase": phase.phase_name,
                "status": phase.status,
                "objective": phase.objective_value,
                "duration_ms": phase.duration_ms,
                "constraints": phase.details.get("constraint_count"),
                "variables": phase.details.get("variable_count"),
            }
            for phase in run.phases
        ]
    )
    if frame.empty:
        return frame
    frame["phase"] = frame["phase"].map(_friendly_phase_name)
    return frame.rename(
        columns={
            "phase": "Phase",
            "status": "Status",
            "objective": "Objective Value",
            "duration_ms": "Duration (ms)",
            "constraints": "Constraints",
            "variables": "Variables",
        }
    )


def _recommendation_table(run) -> pd.DataFrame:
    frame = pd.DataFrame(run.selections)
    if frame.empty:
        return frame
    if "role" not in frame.columns:
        frame["role"] = "unknown"
    if "archetype" not in frame.columns:
        frame["archetype"] = "unknown"
    frame = frame.rename(
        columns={
            "upc": "SKU",
            "role": "Role",
            "archetype": "Archetype",
            "candidate_price": "Recommended Price",
            "discount_pct": "Discount",
            "gross_profit": "Expected Gross Profit",
            "expected_units": "Expected Units",
            "ending_inventory_units": "Ending Inventory",
            "competitor_gap": "Weighted Competitor Gap",
        }
    )
    frame["Discount"] = frame["Discount"].map(_format_pct)
    return frame.sort_values(["Role", "Discount", "Expected Gross Profit"], ascending=[True, False, False])


def _friendly_phase_name(raw_phase: str) -> str:
    if raw_phase.endswith("competitor_gap"):
        return "1. Competitor position"
    if raw_phase.endswith("gross_profit"):
        return "2. Gross profit"
    if raw_phase.endswith("discount_depth"):
        return "3. Shallower discount"
    return raw_phase


def _mix_table(run, column: str, title: str) -> pd.DataFrame:
    frame = pd.DataFrame(run.selections)
    if frame.empty or column not in frame.columns:
        return pd.DataFrame(columns=[title, "SKU Count"])
    counts = frame[column].fillna("unknown").value_counts().rename_axis(title).reset_index(name="SKU Count")
    return counts


def _scenario_explainer(scenario_row) -> str:
    return SCENARIO_GUIDE.get(
        scenario_row["scenario_id"],
        "This scenario uses the same public-data anchor and deterministic synthetic context, then applies a different inventory and competitive profile.",
    )


def _role_archetype_summary(run) -> str:
    frame = pd.DataFrame(run.selections)
    if frame.empty:
        return "No selections available."
    role_mix = frame["role"].fillna("unknown").value_counts().to_dict() if "role" in frame.columns else {}
    archetype_mix = frame["archetype"].fillna("unknown").value_counts().to_dict() if "archetype" in frame.columns else {}
    return f"Role mix: {role_mix}. Archetype mix: {archetype_mix}."


def _selected_sku_explainer(dossier: dict[str, object]) -> str:
    selected = dossier["selected"]
    current = dossier["current"]
    local_best = dossier["local_best_feasible"]
    return (
        f"Selected means the portfolio chose {selected['discount_pct']:.0%} for this SKU. "
        f"Current is the baseline price point we compare against. "
        f"Local best feasible is the single-SKU gross-profit winner after hard rules, before the rest of the portfolio is considered. "
        f"For this SKU, expected gross profit moves from {_format_currency(float(current['gross_profit']))} at current price "
        f"to {_format_currency(float(selected['gross_profit']))} at the selected point. "
        f"The local best feasible point would be {_format_currency(float(local_best['gross_profit']))}."
    )


def _alternate_candidates_table(alternatives: pd.DataFrame) -> pd.DataFrame:
    frame = alternatives.rename(
        columns={
            "discount_pct": "Discount",
            "candidate_price": "Candidate Price",
            "gross_profit": "Expected Gross Profit",
            "expected_units": "Expected Units",
            "effective_hard_valid": "Hard-Rule Valid",
            "reason": "Invalid Reason",
            "is_selected": "Selected",
            "is_current": "Current",
        }
    ).copy()
    if "Discount" in frame.columns:
        frame["Discount"] = frame["Discount"].map(_format_pct)
    return frame


def main() -> None:
    st.markdown(
        """
        <div class="hero">
            <div class="eyebrow">Explainable Promotion Planning</div>
            <h1>Pricing Optimization Workbench</h1>
            <p>Official plans stay frozen. Every answer in the assistant is grounded in solver output or a separate what-if re-solve.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not DB_PATH.exists():
        st.error("Database not found. Run the ingest and scenario generation scripts first.")
        return

    with closing(get_conn()) as conn:
        scenarios = _load_scenarios(conn)
        if scenarios.empty:
            st.error("No scenarios found. Run `uv run python generate_demo_context.py` first.")
            return

        scenario_options = scenarios["scenario_id"].tolist()
        default_index = 0
        selected_scenario = st.sidebar.selectbox("Scenario", scenario_options, index=default_index)
        scenario_row = scenarios[scenarios["scenario_id"] == selected_scenario].iloc[0]

        planner = PricingDecisionService(conn)
        conversation = PricingConversationService(planner)
        plan = planner.build_plan_bundle(selected_scenario)

        st.sidebar.caption(f"Profile: {scenario_row['profile_id']}")
        st.sidebar.caption(f"Budget limit: {_format_pct(float(scenario_row['budget_pct']))}")
        st.sidebar.caption(f"Safety stock: {_format_pct(float(scenario_row['safety_stock_pct']))}")
        st.sidebar.markdown("`Official proposal` is the fixed reference. `What-if simulation` never overwrites it.")
        with st.sidebar.expander("Scenario guide", expanded=False):
            st.write(_scenario_explainer(scenario_row))
            st.write("This demo starts after demand forecasting. The solver receives precomputed price-response candidates rather than training a forecasting model inside the app.")
        with st.sidebar.expander("Glossary", expanded=False):
            st.dataframe(pd.DataFrame(GLOSSARY_ROWS), use_container_width=True, hide_index=True)

        official = plan.official.summary
        comparison_gp = official["total_gross_profit"] - plan.current_price.summary["total_gross_profit"]
        cols = st.columns(5)
        cols[0].metric("Official GP", f"{official['total_gross_profit']:.2f}")
        cols[1].metric("Revenue", f"{official['total_revenue']:.2f}")
        cols[2].metric("Promoted SKUs", f"{official['promoted_products']}")
        cols[3].metric("Budget Used", _format_pct(float(official["budget_utilization_pct"])))
        cols[4].metric("GP vs Current", f"{comparison_gp:.2f}")
        st.caption(
            "Gross profit is the optimization value most business users care about. "
            "Budget used is markdown spend divided by list-price revenue. "
            "GP vs Current shows whether the campaign outperforms leaving all current prices unchanged."
        )

        overview_tab, sku_tab, chat_tab, guide_tab = st.tabs(["Plan Overview", "SKU Inspector", "Assistant", "Guide"])

        with overview_tab:
            st.subheader("What this scenario is")
            st.write(_scenario_explainer(scenario_row))
            st.caption(
                "The public workbook gives us product, price, and sales history. "
                "Costs, inventory, competitor prices, roles, and demand-response candidates are synthetic but deterministic."
            )

            st.subheader("Benchmark view")
            st.caption(
                "Read this table left to right. Gross profit and revenue are business outputs. "
                "Weighted competitor gap is a penalty-style score where lower is better. "
                "The official proposal intentionally accepts less profit than the profit-first benchmark when it improves price position."
            )
            st.dataframe(_bundle_to_benchmark_table(plan), use_container_width=True, hide_index=True)
            with st.expander("What each benchmark means", expanded=False):
                st.dataframe(pd.DataFrame(BENCHMARK_GUIDE), use_container_width=True, hide_index=True)

            left, right = st.columns([1.3, 1])
            with left:
                st.subheader("Official recommendations")
                st.caption(
                    "Each row is the selected price point for one SKU. "
                    "Role and archetype are synthetic commercial labels used to make the scenario realistic enough for pricing trade-offs."
                )
                st.dataframe(_recommendation_table(plan.official), use_container_width=True, hide_index=True)
                mix_cols = st.columns(2)
                with mix_cols[0]:
                    st.caption("Role mix in the selected plan")
                    st.dataframe(_mix_table(plan.official, "role", "Role"), use_container_width=True, hide_index=True)
                with mix_cols[1]:
                    st.caption("Archetype mix in the selected plan")
                    st.dataframe(_mix_table(plan.official, "archetype", "Archetype"), use_container_width=True, hide_index=True)
                st.caption(_role_archetype_summary(plan.official))
            with right:
                st.subheader("OR run status")
                st.dataframe(_phase_table(plan.official), use_container_width=True, hide_index=True)
                with st.expander("What the solver phases mean", expanded=False):
                    st.dataframe(
                        pd.DataFrame(
                            [{"phase": key, "meaning": value} for key, value in PHASE_GUIDE.items()]
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )
                st.caption(f"Official run id: `{plan.official.run_id}`")
                st.caption(f"Profit-first benchmark run id: `{plan.profit_first.run_id}`")

        with sku_tab:
            st.subheader("Decision inspector")
            sku_options = [row["upc"] for row in plan.official.selections]
            selected_upc = st.selectbox("SKU", sku_options, key=f"sku_{selected_scenario}")
            dossier = planner.get_sku_dossier(plan.official.run_id, selected_upc)
            st.caption(
                "This panel compares three things for one SKU: the selected portfolio choice, the current baseline price, and the SKU-local best gross-profit point that still passes hard rules."
            )

            metric_cols = st.columns(3)
            metric_cols[0].metric("Selected Discount", _format_pct(float(dossier["selected"]["discount_pct"])))
            metric_cols[0].caption(f"Price {dossier['selected']['candidate_price']:.2f}")
            metric_cols[1].metric("Current Discount", _format_pct(float(dossier["current"]["discount_pct"])))
            metric_cols[1].caption(f"Price {dossier['current']['candidate_price']:.2f}")
            metric_cols[2].metric("Local Best Feasible", _format_pct(float(dossier["local_best_feasible"]["discount_pct"])))
            metric_cols[2].caption(f"Price {dossier['local_best_feasible']['candidate_price']:.2f}")
            st.info(_selected_sku_explainer(dossier))

            alternatives = pd.DataFrame(dossier["alternatives"])
            chart_frame = alternatives.copy()
            chart_frame["discount_label"] = (chart_frame["discount_pct"] * 100).round(0).astype(int).astype(str) + "%"
            chart_frame = chart_frame.set_index("discount_label")

            chart_left, chart_right = st.columns(2)
            with chart_left:
                st.caption("Expected gross profit by allowed discount. Higher is better, but the final portfolio may still choose another point because of competitor or budget trade-offs.")
                st.line_chart(chart_frame[["gross_profit"]], color=["#d96c3c"], height=260)
            with chart_right:
                st.caption("Expected units by allowed discount. This comes from the synthetic price-response object, not a fitted model inside this app.")
                st.line_chart(chart_frame[["expected_units"]], color=["#447c5d"], height=260)

            st.caption(
                "Hard-Rule Valid tells you whether a candidate survives margin, inventory, and maximum-discount checks. "
                "If it is false, Invalid Reason explains the blocker."
            )
            st.dataframe(_alternate_candidates_table(alternatives), use_container_width=True, hide_index=True)

        with chat_tab:
            st.subheader("Decision assistant")
            st.caption(
                "This is a bounded assistant, not an open chatbot. It can summarize the proposal, explain one SKU, compare against another discrete discount, or run a safe what-if in a separate child solve."
            )
            sample_upc = plan.official.selections[0]["upc"] if plan.official.selections else "SKU"
            sample_discount = int(round(plan.official.selections[0]["discount_pct"] * 100)) if plan.official.selections else 15
            alternate_discount = 0 if sample_discount != 0 else 5
            example_cols = st.columns(3)
            example_cols[0].code("Summarize the proposal")
            example_cols[1].code(f"Why is SKU {sample_upc} at {sample_discount}%?")
            example_cols[2].code(f"What if we force {alternate_discount}% for SKU {sample_upc}?")
            st.caption(
                "Supported rule what-ifs: budget %, safety-stock %, min-margin % for one SKU, and competitor-tolerance % for one SKU. "
                "The official proposal never changes when you ask these questions."
            )

            chat_key = f"chat_history_{selected_scenario}"
            if chat_key not in st.session_state:
                st.session_state[chat_key] = []

            for turn in st.session_state[chat_key]:
                with st.chat_message("user"):
                    st.write(turn["question"])
                with st.chat_message("assistant"):
                    st.write(turn["response_text"])
                    if turn["intent"]["intent"] in {"OVERRIDE_WHAT_IF", "RULE_WHAT_IF"} and "comparison" in turn["evidence"]:
                        st.caption(f"Official proposal unchanged. What-if run id: `{turn['evidence']['what_if_run_id']}`")
                        if turn["evidence"]["comparison"].get("comparable") is False:
                            infeasibility = turn["evidence"].get("infeasibility") or {}
                            st.warning("What-if scenario is infeasible under the current hard rules.")
                            conflict_rows = pd.DataFrame(infeasibility.get("lock_conflicts", []))
                            if not conflict_rows.empty:
                                st.dataframe(conflict_rows, use_container_width=True, hide_index=True)
                            violation_rows = pd.DataFrame(infeasibility.get("hard_violation_examples", []))
                            if not violation_rows.empty:
                                st.dataframe(violation_rows, use_container_width=True, hide_index=True)
                        else:
                            changed = pd.DataFrame(turn["evidence"]["comparison"]["changed_skus"])
                            if not changed.empty:
                                st.dataframe(changed, use_container_width=True, hide_index=True)
                            summary_delta = turn["evidence"]["comparison"].get("summary_delta")
                            if summary_delta:
                                delta_frame = pd.DataFrame(
                                    [{"metric": key, "delta": value} for key, value in summary_delta.items()]
                                )
                                st.dataframe(delta_frame, use_container_width=True, hide_index=True)

                    with st.expander("Evidence payload", expanded=False):
                        st.json(turn["evidence"])

            prompt = st.chat_input("Ask about the proposal or run a bounded what-if...")
            if prompt:
                turn = conversation.handle_question(plan, prompt)
                st.session_state[chat_key].append(
                    {
                        "question": prompt,
                        "response_text": turn.response_text,
                        "intent": turn.intent,
                        "evidence": turn.evidence,
                    }
                )
                st.rerun()

        with guide_tab:
            st.subheader("How to read this app")
            st.write(
                "This demo is about pricing optimization after demand modeling. "
                "We assume an upstream model has already produced demand at a small set of allowed discount points."
            )

            guide_cols = st.columns(2)
            with guide_cols[0]:
                st.markdown("**Data provenance**")
                st.dataframe(pd.DataFrame(DATA_PROVENANCE_ROWS), use_container_width=True, hide_index=True)
            with guide_cols[1]:
                st.markdown("**Core terms**")
                st.dataframe(pd.DataFrame(GLOSSARY_ROWS), use_container_width=True, hide_index=True)

            lower_cols = st.columns(2)
            with lower_cols[0]:
                st.markdown("**Strategic roles**")
                st.dataframe(
                    pd.DataFrame([{"role": key, "meaning": value} for key, value in ROLE_GUIDE.items()]),
                    use_container_width=True,
                    hide_index=True,
                )
            with lower_cols[1]:
                st.markdown("**Synthetic archetypes**")
                st.dataframe(
                    pd.DataFrame([{"archetype": key, "meaning": value} for key, value in ARCHETYPE_GUIDE.items()]),
                    use_container_width=True,
                    hide_index=True,
                )

            st.markdown("**What the solver is doing**")
            st.write(
                "For each SKU, the solver picks exactly one allowed discount from a finite menu. "
                "It enforces hard rules such as budget, minimum margin, and inventory protection. "
                "Then it solves lexicographically: competitor position first, gross profit second, and shallower discounts third."
            )
            st.caption(
                "If something looks weird, the most common reasons are: the plan is protecting inventory, the scenario is prioritizing competitor position, or the current price baseline is already quite profitable."
            )


if __name__ == "__main__":
    main()
