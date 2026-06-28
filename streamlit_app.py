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
    frame["budget_pct"] = frame["budget_pct"].map(_format_pct)
    return frame


def _phase_table(run) -> pd.DataFrame:
    return pd.DataFrame(
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
            "candidate_price": "price",
            "discount_pct": "discount",
            "gross_profit": "gross_profit",
            "expected_units": "expected_units",
            "ending_inventory_units": "ending_inventory",
            "competitor_gap": "competitor_gap",
        }
    )
    frame["discount"] = frame["discount"].map(_format_pct)
    return frame.sort_values(["role", "discount", "gross_profit"], ascending=[True, False, False])


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

        official = plan.official.summary
        comparison_gp = official["total_gross_profit"] - plan.current_price.summary["total_gross_profit"]
        cols = st.columns(5)
        cols[0].metric("Official GP", f"{official['total_gross_profit']:.2f}")
        cols[1].metric("Revenue", f"{official['total_revenue']:.2f}")
        cols[2].metric("Promoted SKUs", f"{official['promoted_products']}")
        cols[3].metric("Budget Used", _format_pct(float(official["budget_utilization_pct"])))
        cols[4].metric("GP vs Current", f"{comparison_gp:.2f}")

        overview_tab, sku_tab, chat_tab = st.tabs(["Plan Overview", "SKU Inspector", "Assistant"])

        with overview_tab:
            st.subheader("Benchmark view")
            st.dataframe(_bundle_to_benchmark_table(plan), use_container_width=True, hide_index=True)

            left, right = st.columns([1.3, 1])
            with left:
                st.subheader("Official recommendations")
                st.dataframe(_recommendation_table(plan.official), use_container_width=True, hide_index=True)
            with right:
                st.subheader("OR run status")
                st.dataframe(_phase_table(plan.official), use_container_width=True, hide_index=True)
                st.caption(f"Official run id: `{plan.official.run_id}`")
                st.caption(f"Profit-first benchmark run id: `{plan.profit_first.run_id}`")

        with sku_tab:
            st.subheader("Decision inspector")
            sku_options = [row["upc"] for row in plan.official.selections]
            selected_upc = st.selectbox("SKU", sku_options, key=f"sku_{selected_scenario}")
            dossier = planner.get_sku_dossier(plan.official.run_id, selected_upc)

            metric_cols = st.columns(3)
            metric_cols[0].metric("Selected Discount", _format_pct(float(dossier["selected"]["discount_pct"])))
            metric_cols[0].caption(f"Price {dossier['selected']['candidate_price']:.2f}")
            metric_cols[1].metric("Current Discount", _format_pct(float(dossier["current"]["discount_pct"])))
            metric_cols[1].caption(f"Price {dossier['current']['candidate_price']:.2f}")
            metric_cols[2].metric("Local Best Feasible", _format_pct(float(dossier["local_best_feasible"]["discount_pct"])))
            metric_cols[2].caption(f"Price {dossier['local_best_feasible']['candidate_price']:.2f}")

            alternatives = pd.DataFrame(dossier["alternatives"])
            chart_frame = alternatives.copy()
            chart_frame["discount_label"] = (chart_frame["discount_pct"] * 100).round(0).astype(int).astype(str) + "%"
            chart_frame = chart_frame.set_index("discount_label")

            chart_left, chart_right = st.columns(2)
            with chart_left:
                st.caption("Expected gross profit by allowed discount")
                st.line_chart(chart_frame[["gross_profit"]], color=["#d96c3c"], height=260)
            with chart_right:
                st.caption("Expected units by allowed discount")
                st.line_chart(chart_frame[["expected_units"]], color=["#447c5d"], height=260)

            st.dataframe(
                alternatives.assign(
                    discount_pct=alternatives["discount_pct"].map(_format_pct),
                ),
                use_container_width=True,
                hide_index=True,
            )

        with chat_tab:
            st.subheader("Decision assistant")
            st.caption(
                "Supported intents: plan summary, why this SKU, why not another discrete discount, force a discrete SKU discount, or change one safe rule in a separate what-if run."
            )
            sample_upc = plan.official.selections[0]["upc"] if plan.official.selections else "SKU"
            sample_discount = int(round(plan.official.selections[0]["discount_pct"] * 100)) if plan.official.selections else 15
            alternate_discount = 0 if sample_discount != 0 else 5
            example_cols = st.columns(3)
            example_cols[0].code("Summarize the proposal")
            example_cols[1].code(f"Why is SKU {sample_upc} at {sample_discount}%?")
            example_cols[2].code(f"What if we force {alternate_discount}% for SKU {sample_upc}?")

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


if __name__ == "__main__":
    main()
