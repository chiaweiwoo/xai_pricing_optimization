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
from xai_pricing.planner import PlanBundle, PricingDecisionService


SCENARIO_GUIDE = {
    "balanced_campaign_v1": "Balanced campaign. Inventory is healthy enough to support a broader promotional mix while competitor pressure still matters.",
    "inventory_stress_v1": "Inventory stress campaign. More products need protection because deeper discounts can push ending inventory too close to safety stock.",
    "demo_pricing_v1": "Sandbox campaign generated from the same synthetic logic as the main demos.",
}

ROLE_GUIDE = {
    "kvi": "Key value item. A visible item where shopper price perception matters.",
    "traffic_driver": "A trip-building item where sharper discounts can be acceptable.",
    "margin_driver": "A product where margin protection matters more than aggressive discounting.",
    "long_tail": "A lower-priority item with less strategic weight than the headline products.",
}

ARCHETYPE_GUIDE = {
    "competitor_pressure": "Competitor is priced aggressively, so price position matters more.",
    "low_inventory": "Inventory is tight, so deep discounts are risky.",
    "overstock": "There is room to push more units without threatening stock.",
    "margin_constrained": "Cost structure limits how far price can move safely.",
    "promotion_opportunity": "Demand is responsive enough that a moderate discount can unlock useful volume.",
    "neutral": "No single synthetic pressure dominates this product.",
}

DATA_PROVENANCE_ROWS = [
    {
        "Field group": "Public data",
        "Details": "Products, historical weekly units, spend, shelf price, base price, and promo flags from dunnhumby Breakfast at the Frat.",
    },
    {
        "Field group": "Synthetic but deterministic",
        "Details": "Unit cost, competitor price, inventory, strategic role, archetype, elasticity parameters, and candidate price-point outcomes.",
    },
    {
        "Field group": "Optimization input",
        "Details": "A discrete menu of discount choices: 0%, 5%, 10%, 15%, 20%, and 25%, each with projected units, revenue, gross profit, and inventory impact.",
    },
]

GLOSSARY_ROWS = [
    {"Term": "Official proposal", "Meaning": "The fixed reference recommendation used for explanation and comparison."},
    {"Term": "Profit-first feasible", "Meaning": "A comparison plan that respects the same hard rules but maximizes gross profit first."},
    {"Term": "Current-price baseline", "Meaning": "The outcome if every product keeps its current price point."},
    {"Term": "Theoretical ceiling", "Meaning": "The best product-level gross-profit point without the full portfolio trade-offs."},
    {"Term": "Weighted competitor gap", "Meaning": "A penalty score for pricing above tolerated competitor position on important products. Lower is better."},
    {"Term": "Safety stock", "Meaning": "Inventory buffer the solver tries to protect so a promotion does not create stockout risk."},
]

PHASE_GUIDE = {
    "competitor_gap": "Minimize weighted competitor gap to protect price perception.",
    "gross_profit": "Maximize gross profit after honoring the competitor-position result.",
    "discount_depth": "Prefer shallower discounts when earlier phases tie closely enough.",
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
        --bg-top: #f6ecdf;
        --bg-bottom: #fffaf5;
        --surface: rgba(255, 252, 247, 0.9);
        --surface-strong: rgba(255, 248, 239, 0.98);
        --surface-soft: rgba(255, 243, 230, 0.72);
        --ink: #1f1d1a;
        --muted: #675f55;
        --accent: #c75b2d;
        --accent-strong: #a9481d;
        --accent-soft: rgba(199, 91, 45, 0.12);
        --green-soft: rgba(73, 119, 92, 0.12);
        --border: rgba(103, 76, 52, 0.14);
        --warning: #8a4d18;
    }

    html, body, [class*="css"] {
        font-family: 'IBM Plex Sans', sans-serif;
        color: var(--ink);
    }

    .stApp {
        background:
            radial-gradient(circle at top left, rgba(199, 91, 45, 0.12), transparent 30%),
            radial-gradient(circle at top right, rgba(73, 119, 92, 0.10), transparent 24%),
            linear-gradient(180deg, var(--bg-top), var(--bg-bottom));
    }

    .block-container {
        padding-top: 1.8rem;
        padding-bottom: 2rem;
    }

    .hero-card, .note-card, .journey-card, .assistant-card {
        background: linear-gradient(135deg, rgba(255,255,255,0.94), rgba(255,244,232,0.88));
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: 0 18px 48px rgba(103, 76, 52, 0.08);
    }

    .hero-card {
        padding: 1.5rem 1.6rem 1.35rem 1.6rem;
        margin-bottom: 1rem;
    }

    .note-card, .journey-card, .assistant-card {
        padding: 1rem 1.1rem;
        margin-bottom: 0.8rem;
    }

    .eyebrow {
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--accent);
        font-size: 0.77rem;
        font-weight: 700;
    }

    .hero-title {
        font-size: 2.1rem;
        line-height: 1.1;
        margin: 0.18rem 0 0.25rem 0;
    }

    .hero-copy, .note-copy {
        color: var(--muted);
        margin: 0;
    }

    .status-row {
        display: flex;
        gap: 0.55rem;
        flex-wrap: wrap;
        margin-bottom: 0.65rem;
    }

    .status-pill {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.28rem 0.68rem;
        font-size: 0.78rem;
        font-weight: 600;
        border: 1px solid var(--border);
        background: var(--surface-soft);
    }

    .status-pill.warning {
        background: rgba(201, 118, 44, 0.14);
        color: var(--warning);
    }

    .status-pill.good {
        background: var(--green-soft);
        color: #35634a;
    }

    .kpi-card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 22px;
        padding: 1rem 1rem 0.95rem 1rem;
        box-shadow: 0 10px 28px rgba(103, 76, 52, 0.05);
        min-height: 148px;
        margin-bottom: 0.7rem;
    }

    .kpi-label {
        color: var(--muted);
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        margin-bottom: 0.45rem;
    }

    .kpi-value {
        font-size: 1.6rem;
        font-weight: 700;
        line-height: 1.1;
        margin-bottom: 0.35rem;
    }

    .kpi-delta {
        font-size: 0.9rem;
        color: var(--muted);
    }

    div[data-testid="stDataFrame"], div[data-testid="stExpander"], div[data-testid="stVerticalBlockBorderWrapper"] {
        background: var(--surface-strong);
        border-radius: 18px;
        border: 1px solid var(--border);
    }

    .stage-caption {
        color: var(--muted);
        margin: 0.2rem 0 0.8rem 0;
    }

    .starter-button button {
        width: 100%;
        min-height: 3rem;
        white-space: normal;
    }

    .assistant-answer h4 {
        margin-bottom: 0.25rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _format_pct(value: float) -> str:
    return f"{value:.1%}"


def _format_currency(value: float) -> str:
    amount = float(value)
    prefix = "-" if amount < 0 else ""
    return f"{prefix}${abs(amount):,.2f}"


def _format_count(value: float | int) -> str:
    return f"{int(round(float(value))):,}"


def _format_decimal(value: float | int, digits: int = 4) -> str:
    return f"{float(value):,.{digits}f}"


def _format_quantity(value: float | int) -> str:
    return f"{float(value):,.2f}"


def _apply_formats(
    frame: pd.DataFrame,
    *,
    currency_cols: list[str] | None = None,
    pct_cols: list[str] | None = None,
    decimal_cols: list[str] | None = None,
    quantity_cols: list[str] | None = None,
    count_cols: list[str] | None = None,
) -> pd.DataFrame:
    formatted = frame.copy()
    for col in currency_cols or []:
        if col in formatted.columns:
            formatted[col] = formatted[col].map(_format_currency)
    for col in pct_cols or []:
        if col in formatted.columns:
            formatted[col] = formatted[col].map(_format_pct)
    for col in decimal_cols or []:
        if col in formatted.columns:
            formatted[col] = formatted[col].map(_format_decimal)
    for col in quantity_cols or []:
        if col in formatted.columns:
            formatted[col] = formatted[col].map(_format_quantity)
    for col in count_cols or []:
        if col in formatted.columns:
            formatted[col] = formatted[col].map(_format_count)
    return formatted


def _load_scenarios(conn) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT scenario_id, scenario_name, profile_id, budget_pct, safety_stock_pct
        FROM scenarios
        ORDER BY created_at DESC, scenario_id
        """,
        conn,
    )


def _scenario_options(scenarios: pd.DataFrame) -> dict[str, str]:
    return {
        row["scenario_id"]: f"{row['scenario_name']} ({row['scenario_id']})"
        for _, row in scenarios.iterrows()
    }


def _status_badge(label: str, kind: str = "good") -> str:
    return f'<span class="status-pill {kind}">{label}</span>'


def _kpi_card(label: str, value: str, delta: str, tone: str = "default") -> None:
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value">{value}</div>
            <div class="kpi-delta">{delta}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _note_card(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="note-card">
            <div class="eyebrow">{title}</div>
            <p class="note-copy">{body}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _friendly_phase_name(raw_phase: str) -> str:
    if raw_phase.endswith("competitor_gap"):
        return "1. Competitor position"
    if raw_phase.endswith("gross_profit"):
        return "2. Gross profit"
    if raw_phase.endswith("discount_depth"):
        return "3. Shallower discount"
    return raw_phase


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
    frame = frame.rename(
        columns={
            "phase": "Phase",
            "status": "Status",
            "objective": "Objective Value",
            "duration_ms": "Duration (ms)",
            "constraints": "Constraints",
            "variables": "Variables",
        }
    )
    return _apply_formats(
        frame,
        decimal_cols=["Objective Value"],
        count_cols=["Duration (ms)", "Constraints", "Variables"],
    )


def _benchmark_table(plan: PlanBundle) -> pd.DataFrame:
    rows = [
        {
            "Plan": "Official proposal",
            "Status": plan.official.status,
            "Gross Profit": plan.official.summary["total_gross_profit"],
            "Revenue": plan.official.summary["total_revenue"],
            "Budget Used": plan.official.summary["budget_utilization_pct"],
            "Weighted Competitor Gap": plan.official.summary["weighted_competitor_gap"],
        },
        {
            "Plan": "Profit-first feasible",
            "Status": plan.profit_first.status,
            "Gross Profit": plan.profit_first.summary["total_gross_profit"],
            "Revenue": plan.profit_first.summary["total_revenue"],
            "Budget Used": plan.profit_first.summary["budget_utilization_pct"],
            "Weighted Competitor Gap": plan.profit_first.summary["weighted_competitor_gap"],
        },
        {
            "Plan": "Current-price baseline",
            "Status": plan.current_price.status,
            "Gross Profit": plan.current_price.summary["total_gross_profit"],
            "Revenue": plan.current_price.summary["total_revenue"],
            "Budget Used": plan.current_price.summary["budget_utilization_pct"],
            "Weighted Competitor Gap": plan.current_price.summary["weighted_competitor_gap"],
        },
        {
            "Plan": "Theoretical ceiling",
            "Status": plan.theoretical_ceiling.status,
            "Gross Profit": plan.theoretical_ceiling.summary["total_gross_profit"],
            "Revenue": plan.theoretical_ceiling.summary["total_revenue"],
            "Budget Used": plan.theoretical_ceiling.summary["budget_utilization_pct"],
            "Weighted Competitor Gap": plan.theoretical_ceiling.summary["weighted_competitor_gap"],
        },
    ]
    return _apply_formats(
        pd.DataFrame(rows),
        currency_cols=["Gross Profit", "Revenue"],
        pct_cols=["Budget Used"],
        decimal_cols=["Weighted Competitor Gap"],
    )


def _recommendation_table(plan: PlanBundle) -> pd.DataFrame:
    frame = pd.DataFrame(plan.official.selections)
    if frame.empty:
        return frame
    catalog_map = {row["upc"]: row for row in plan.catalog}
    frame["category"] = frame["upc"].map(lambda upc: catalog_map.get(upc, {}).get("category"))
    frame["sub_category"] = frame["upc"].map(lambda upc: catalog_map.get(upc, {}).get("sub_category"))
    frame = frame.rename(
        columns={
            "product_label": "Product",
            "upc": "UPC",
            "category": "Category",
            "sub_category": "Sub-category",
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
    frame = frame[
        [
            "Product",
            "UPC",
            "Category",
            "Sub-category",
            "Role",
            "Archetype",
            "Discount",
            "Recommended Price",
            "Expected Gross Profit",
            "Expected Units",
            "Ending Inventory",
            "Weighted Competitor Gap",
        ]
    ].sort_values(["Category", "Role", "Discount", "Expected Gross Profit"], ascending=[True, True, False, False])
    return _apply_formats(
        frame,
        currency_cols=["Recommended Price", "Expected Gross Profit"],
        pct_cols=["Discount"],
        decimal_cols=["Weighted Competitor Gap"],
        quantity_cols=["Expected Units", "Ending Inventory"],
    )


def _alternate_candidates_table(alternatives: pd.DataFrame) -> pd.DataFrame:
    frame = alternatives.rename(
        columns={
            "discount_pct": "Discount",
            "candidate_price": "Candidate Price",
            "gross_profit": "Expected Gross Profit",
            "expected_units": "Expected Units",
            "revenue": "Revenue",
            "ending_inventory_units": "Ending Inventory",
            "gross_margin_pct": "Gross Margin",
            "competitor_index": "Competitor Index",
            "competitor_gap": "Weighted Competitor Gap",
            "effective_hard_valid": "Hard-Rule Valid",
            "reason": "Invalid Reason",
            "is_selected": "Selected",
            "is_current": "Current",
        }
    ).copy()
    return _apply_formats(
        frame,
        currency_cols=["Candidate Price", "Expected Gross Profit", "Revenue"],
        pct_cols=["Discount", "Gross Margin"],
        decimal_cols=["Competitor Index", "Weighted Competitor Gap"],
        quantity_cols=["Expected Units", "Ending Inventory"],
    )


def _format_delta_table(summary_delta: dict[str, float]) -> pd.DataFrame:
    labels = {
        "selected_products": "Selected products",
        "promoted_products": "Promoted products",
        "protected_products": "Protected products",
        "total_revenue": "Revenue",
        "total_gross_profit": "Gross Profit",
        "total_markdown_investment": "Markdown Spend",
        "budget_utilization_pct": "Budget Used",
        "weighted_competitor_gap": "Weighted Competitor Gap",
        "inventory_tight_products": "Inventory-tight products",
    }
    rows = []
    for key, value in summary_delta.items():
        if key in {"total_revenue", "total_gross_profit", "total_markdown_investment"}:
            formatted = _format_currency(value)
        elif key == "budget_utilization_pct":
            formatted = _format_pct(value)
        elif key == "weighted_competitor_gap":
            formatted = _format_decimal(value)
        else:
            formatted = _format_count(value)
        rows.append({"Metric": labels.get(key, key), "Delta vs official": formatted})
    return pd.DataFrame(rows)


def _format_changed_skus(changed: pd.DataFrame) -> pd.DataFrame:
    if changed.empty:
        return changed
    frame = changed.rename(
        columns={
            "product_label": "Product",
            "upc": "UPC",
            "category": "Category",
            "discount_pct_base": "Official Discount",
            "discount_pct_candidate": "What-If Discount",
            "gross_profit_delta": "Gross Profit Delta",
            "expected_units_delta": "Expected Units Delta",
            "competitor_gap_delta": "Weighted Competitor Gap Delta",
        }
    )
    return _apply_formats(
        frame,
        pct_cols=["Official Discount", "What-If Discount"],
        currency_cols=["Gross Profit Delta"],
        decimal_cols=["Weighted Competitor Gap Delta"],
        quantity_cols=["Expected Units Delta"],
    )


def _scenario_explainer(scenario_id: str) -> str:
    return SCENARIO_GUIDE.get(
        scenario_id,
        "This scenario uses the same public-data anchor and deterministic synthetic context, then changes the competitive and inventory profile.",
    )


def _stage_options() -> list[str]:
    return [
        "1. Decision Brief",
        "2. Product Decisions",
        "3. Ask & Simulate",
        "4. Method & Audit",
    ]


def _starter_questions(plan: PlanBundle) -> list[str]:
    featured = sorted(
        plan.official.selections,
        key=lambda row: (-float(row["discount_pct"]), -float(row["gross_profit"])),
    )
    sample = featured[0] if featured else None
    if sample is None:
        return ["Summarize the proposal"]
    alt = 0 if abs(float(sample["discount_pct"])) > 1e-9 else 5
    return [
        "Summarize the proposal",
        f"Why is {sample['product_label']} at {int(round(float(sample['discount_pct']) * 100))}%?",
        f"Why not {alt}% for {sample['product_label']}?",
        f"What if we force {alt}% for {sample['product_label']}?",
        "What if budget becomes 8%?",
    ]


def _render_hero(plan: PlanBundle) -> None:
    brief = plan.brief
    status_kind = "warning" if brief["status"] == "review_required" else "good"
    strategy_label = "Price-position strategy" if brief["strategy"] == "price_position_strategy" else "Balanced strategy"
    st.markdown(
        f"""
        <div class="hero-card">
            <div class="status-row">
                {_status_badge(strategy_label, 'good')}
                {_status_badge('Profit trade-off requires review' if brief['status'] == 'review_required' else 'Commercially on track', status_kind)}
                {_status_badge(f"Scenario: {plan.scenario['scenario_name']}", 'good')}
            </div>
            <div class="eyebrow">Guided Pricing Decision</div>
            <div class="hero-title">{brief['headline']}</div>
            <p class="hero-copy">
                {brief['tradeoff_summary']} This demo starts after demand forecasting:
                the optimizer receives precomputed demand responses at discrete discount choices.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_decision_brief(plan: PlanBundle) -> None:
    brief = plan.brief
    official = plan.official.summary
    current = plan.current_price.summary

    st.markdown('<p class="stage-caption">Start here. This page explains what decision is being made, why the official proposal exists, and what its main downside is.</p>', unsafe_allow_html=True)

    top_cols = st.columns(3)
    with top_cols[0]:
        _kpi_card(
            "Official Gross Profit",
            _format_currency(official["total_gross_profit"]),
            f"Current-price baseline: {_format_currency(current['total_gross_profit'])}",
        )
    with top_cols[1]:
        _kpi_card(
            "Gross Profit vs Current",
            _format_currency(brief["profit_vs_current"]),
            "Negative means the campaign sacrifices profit versus keeping today’s prices.",
        )
    with top_cols[2]:
        _kpi_card(
            "Revenue vs Current",
            _format_currency(brief["revenue_vs_current"]),
            "Positive means the campaign is expected to sell more value through the basket.",
        )

    bottom_cols = st.columns(3)
    with bottom_cols[0]:
        _kpi_card(
            "Promoted / Protected",
            f"{_format_count(brief['promoted_products'])} / {_format_count(brief['protected_products'])}",
            f"Across {_format_count(brief['selected_products'])} products in the portfolio.",
        )
    with bottom_cols[1]:
        _kpi_card(
            "Budget Used",
            _format_pct(official["budget_utilization_pct"]),
            f"Limit: {_format_pct(brief['budget_limit_pct'])}. {'Budget is effectively binding.' if brief['budget_binding'] else 'Some budget headroom remains.'}",
        )
    with bottom_cols[2]:
        _kpi_card(
            "Competitor Gap Improvement",
            _format_decimal(brief["gap_improvement_vs_current"]),
            f"Current gap: {_format_decimal(current['weighted_competitor_gap'])}. Lower is better.",
        )

    note_cols = st.columns(3)
    with note_cols[0]:
        _note_card("What We Are Deciding", brief["decision"])
    with note_cols[1]:
        _note_card(
            "Why This Proposal Exists",
            "The official solve protects competitor price position first, then recovers as much gross profit as it can within that result.",
        )
    with note_cols[2]:
        _note_card(
            "What To Review",
            f"There are {_format_count(brief['inventory_tight_products'])} inventory-tight products. These are the first places to inspect when a discount looks shallower than expected.",
        )

    st.subheader("Three things to take away")
    takeaway_rows = [
        "The official proposal is a competitor-first campaign, not the pure profit-maximizing feasible plan.",
        f"Versus current prices, the plan changes gross profit by {_format_currency(brief['profit_vs_current'])} and revenue by {_format_currency(brief['revenue_vs_current'])}.",
        "Versus the profit-first benchmark, the plan accepts less profit so weighted competitor gap can improve materially.",
    ]
    for takeaway in takeaway_rows:
        st.markdown(f"<div style='margin: 0.2rem 0;'>• {takeaway}</div>", unsafe_allow_html=True)

    st.subheader("Benchmark comparison")
    st.caption("This is the fastest way to see the trade-off. Lower weighted competitor gap is better. The theoretical ceiling is not a recommended plan; it is only an upper bound.")
    st.dataframe(_benchmark_table(plan), use_container_width=True, hide_index=True)


def _render_product_decisions(plan: PlanBundle, planner: PricingDecisionService) -> None:
    st.markdown('<p class="stage-caption">Review which products were promoted, which were protected, and how one product behaves across the allowed discount ladder.</p>', unsafe_allow_html=True)

    recommendation_frame = pd.DataFrame(plan.official.selections)
    catalog_map = {row["upc"]: row for row in plan.catalog}
    recommendation_frame["category"] = recommendation_frame["upc"].map(lambda upc: catalog_map.get(upc, {}).get("category"))
    recommendation_frame["role"] = recommendation_frame["upc"].map(lambda upc: catalog_map.get(upc, {}).get("role"))
    recommendation_frame["inventory_tight"] = recommendation_frame["upc"].map(
        lambda upc: catalog_map.get(upc, {}).get("safety_stock_units", 0) >= catalog_map.get(upc, {}).get("on_hand_units", 0) * 0.5
    )

    filter_cols = st.columns(4)
    category_options = sorted([value for value in recommendation_frame["category"].dropna().unique().tolist()])
    role_options = sorted([value for value in recommendation_frame["role"].dropna().unique().tolist()])
    selected_categories = filter_cols[0].multiselect("Category", category_options, default=category_options)
    selected_roles = filter_cols[1].multiselect("Role", role_options, default=role_options)
    promoted_only = filter_cols[2].toggle("Promoted only", value=False)
    inventory_tight_only = filter_cols[3].toggle("Inventory-tight only", value=False)

    filtered = recommendation_frame.copy()
    if selected_categories:
        filtered = filtered[filtered["category"].isin(selected_categories)]
    if selected_roles:
        filtered = filtered[filtered["role"].isin(selected_roles)]
    if promoted_only:
        filtered = filtered[filtered["discount_pct"] > 0]
    if inventory_tight_only:
        filtered = filtered[filtered["inventory_tight"]]

    filtered_plan = PlanBundle(
        scenario_id=plan.scenario_id,
        scenario=plan.scenario,
        official=plan.official.__class__(
            run_id=plan.official.run_id,
            scenario_id=plan.official.scenario_id,
            status=plan.official.status,
            summary=plan.official.summary,
            phases=plan.official.phases,
            diagnostics=plan.official.diagnostics,
            selections=filtered.to_dict("records"),
        ),
        profit_first=plan.profit_first,
        current_price=plan.current_price,
        theoretical_ceiling=plan.theoretical_ceiling,
        brief=plan.brief,
        catalog=plan.catalog,
    )
    st.dataframe(_recommendation_table(filtered_plan), use_container_width=True, hide_index=True)

    product_options = {row["product_label"]: row["upc"] for row in plan.catalog}
    default_label = next((row["product_label"] for row in plan.catalog if row["upc"] in filtered["upc"].tolist()), plan.catalog[0]["product_label"])
    selected_label = st.selectbox("Inspect one product", list(product_options.keys()), index=list(product_options.keys()).index(default_label))
    selected_upc = product_options[selected_label]
    dossier = planner.get_sku_dossier(plan.official.run_id, selected_upc)

    meta_cols = st.columns([1.3, 1, 1])
    with meta_cols[0]:
        st.markdown(f"### {dossier['product']['product_label']}")
        st.caption(
            f"UPC {dossier['upc']} | {dossier['product']['category']} | {dossier['product']['sub_category']} | "
            f"Role: {dossier['product']['role']} | Archetype: {dossier['product']['archetype']}"
        )
    with meta_cols[1]:
        _note_card(
            "Competitor Context",
            f"Competitor price: {_format_currency(dossier['context']['competitor_price'] or 0)}. "
            f"Tolerance: {_format_pct(dossier['context']['competitor_tolerance_pct'] or 0)}. "
            f"Weight: {_format_count(dossier['context']['competitor_weight'] or 0)}.",
        )
    with meta_cols[2]:
        _note_card(
            "Inventory Context",
            f"On hand: {_format_count(dossier['context']['on_hand_units'] or 0)} units. "
            f"Inbound: {_format_count(dossier['context']['inbound_units'] or 0)}. "
            f"Safety stock: {_format_count(dossier['context']['safety_stock_units'] or 0)}.",
        )

    decision_cols = st.columns(3)
    with decision_cols[0]:
        _kpi_card(
            "Selected Discount",
            _format_pct(dossier["selected"]["discount_pct"]),
            f"Price {_format_currency(dossier['selected']['candidate_price'])}",
        )
    with decision_cols[1]:
        _kpi_card(
            "Current Price Point",
            _format_pct(dossier["current"]["discount_pct"]),
            f"Price {_format_currency(dossier['current']['candidate_price'])}",
        )
    with decision_cols[2]:
        _kpi_card(
            "Local Best Feasible",
            _format_pct(dossier["local_best_feasible"]["discount_pct"]),
            f"Price {_format_currency(dossier['local_best_feasible']['candidate_price'])}",
        )

    st.markdown(
        f"""
        <div class="journey-card">
            Selected versus current: gross profit changes by {_format_currency(dossier['selected_vs_current']['gross_profit'])},
            revenue by {_format_currency(dossier['selected_vs_current']['revenue'])}, and weighted competitor gap by
            {_format_decimal(dossier['selected_vs_current']['competitor_gap'])}.
        </div>
        """,
        unsafe_allow_html=True,
    )

    alternatives = pd.DataFrame(dossier["alternatives"])
    chart_frame = alternatives.copy()
    chart_frame["discount_label"] = (chart_frame["discount_pct"] * 100).round(0).astype(int).astype(str) + "%"
    chart_frame = chart_frame.set_index("discount_label")
    chart_cols = st.columns(2)
    with chart_cols[0]:
        st.caption("Expected gross profit by allowed discount. This is the product-level view, before the full portfolio trade-off.")
        st.line_chart(chart_frame[["gross_profit"]], color=["#c75b2d"], height=260)
    with chart_cols[1]:
        st.caption("Expected units by allowed discount. These values come from the prepared demand-response object, not from a live model fit in the app.")
        st.line_chart(chart_frame[["expected_units"]], color=["#49775c"], height=260)

    st.caption("Hard-Rule Valid tells you whether a candidate survives margin, inventory, and maximum-discount checks before the portfolio optimization starts.")
    st.dataframe(_alternate_candidates_table(alternatives), use_container_width=True, hide_index=True)


def _render_assistant_turn(turn: dict[str, object]) -> None:
    presentation = turn["presentation"]
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        st.markdown(f"#### {presentation['headline']}")
        st.write(presentation["summary"])
        if presentation.get("key_points"):
            bullet_html = "".join([f"<li>{point}</li>" for point in presentation["key_points"]])
            st.markdown(f"<ul>{bullet_html}</ul>", unsafe_allow_html=True)
        if presentation.get("caveat"):
            st.caption(f"Caveat: {presentation['caveat']}")
        if presentation.get("suggested_questions"):
            st.caption("Try next: " + " | ".join(presentation["suggested_questions"]))

        comparison = turn["evidence"].get("comparison") if isinstance(turn["evidence"], dict) else None
        if comparison:
            st.caption(f"Official proposal unchanged. Comparison run id: `{turn['evidence'].get('what_if_run_id', 'n/a')}`")
            if comparison.get("comparable") is False:
                st.warning("This what-if scenario is infeasible under the current hard rules.")
                conflicts = pd.DataFrame((turn["evidence"].get("infeasibility") or {}).get("lock_conflicts", []))
                if not conflicts.empty:
                    st.dataframe(conflicts, use_container_width=True, hide_index=True)
            else:
                changed = _format_changed_skus(pd.DataFrame(comparison["changed_skus"]))
                if not changed.empty:
                    st.dataframe(changed, use_container_width=True, hide_index=True)
                if comparison.get("summary_delta"):
                    st.dataframe(_format_delta_table(comparison["summary_delta"]), use_container_width=True, hide_index=True)


def _render_ask_and_simulate(plan: PlanBundle, conversation: PricingConversationService) -> None:
    st.markdown('<p class="stage-caption">Ask in plain language. The assistant first classifies your request into a supported scenario, then answers with solver evidence or a separate what-if re-solve.</p>', unsafe_allow_html=True)

    intro_cols = st.columns(2)
    with intro_cols[0]:
        _note_card(
            "Explain The Fixed Proposal",
            "Ask for a plan summary, why one product got its discount, or why another allowed discount was not chosen.",
        )
    with intro_cols[1]:
        _note_card(
            "Run A Safe What-If",
            "Ask to force one product to another allowed discount, or change one safe rule such as budget or safety stock. The official proposal never changes.",
        )

    st.caption("Example scope: plan summary, why this discount, why not another discrete discount, force one discrete discount, or change budget / safety stock / minimum margin / competitor tolerance.")

    starters = _starter_questions(plan)
    starter_cols = st.columns(len(starters))
    for idx, prompt in enumerate(starters):
        with starter_cols[idx]:
            if st.button(prompt, key=f"starter_{plan.scenario_id}_{idx}", use_container_width=True):
                st.session_state[f"queued_prompt_{plan.scenario_id}"] = prompt

    chat_key = f"chat_history_{plan.scenario_id}"
    if chat_key not in st.session_state:
        st.session_state[chat_key] = []

    for turn in st.session_state[chat_key]:
        _render_assistant_turn(turn)

    queued_prompt = st.session_state.pop(f"queued_prompt_{plan.scenario_id}", None)
    typed_prompt = st.chat_input("Ask about the proposal or test one bounded what-if...")
    prompt = queued_prompt or typed_prompt
    if prompt:
        turn = conversation.handle_question(plan, prompt)
        payload = {
            "question": prompt,
            "response_text": turn.response_text,
            "intent": turn.intent,
            "evidence": turn.evidence,
            "presentation": turn.presentation,
        }
        st.session_state[chat_key].append(payload)
        st.session_state[f"latest_audit_{plan.scenario_id}"] = payload
        st.rerun()


def _render_method_and_audit(plan: PlanBundle) -> None:
    st.markdown('<p class="stage-caption">Use this page when you want solver details, terminology, provenance, or the latest assistant evidence payload.</p>', unsafe_allow_html=True)

    cols = st.columns(2)
    with cols[0]:
        st.subheader("How the solver works")
        st.write(
            "For each product, the solver picks exactly one discrete discount from a prepared menu. "
            "It enforces hard rules such as budget, minimum margin, and inventory protection. "
            "Then it solves lexicographically: competitor position first, gross profit second, and shallower discount depth third."
        )
        st.dataframe(
            pd.DataFrame([{"Phase": key, "Meaning": value} for key, value in PHASE_GUIDE.items()]),
            use_container_width=True,
            hide_index=True,
        )
        st.dataframe(_phase_table(plan.official), use_container_width=True, hide_index=True)
        st.caption(f"Official run id: `{plan.official.run_id}`")
        st.caption(f"Profit-first benchmark run id: `{plan.profit_first.run_id}`")
    with cols[1]:
        st.subheader("Data provenance")
        st.dataframe(pd.DataFrame(DATA_PROVENANCE_ROWS), use_container_width=True, hide_index=True)
        st.subheader("Glossary")
        st.dataframe(pd.DataFrame(GLOSSARY_ROWS), use_container_width=True, hide_index=True)

    lower = st.columns(2)
    with lower[0]:
        st.subheader("Strategic roles")
        st.dataframe(
            pd.DataFrame([{"Role": key, "Meaning": value} for key, value in ROLE_GUIDE.items()]),
            use_container_width=True,
            hide_index=True,
        )
    with lower[1]:
        st.subheader("Synthetic archetypes")
        st.dataframe(
            pd.DataFrame([{"Archetype": key, "Meaning": value} for key, value in ARCHETYPE_GUIDE.items()]),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Benchmark audit table")
    st.dataframe(_benchmark_table(plan), use_container_width=True, hide_index=True)

    latest_audit = st.session_state.get(f"latest_audit_{plan.scenario_id}")
    if latest_audit:
        with st.expander("Latest assistant intent and evidence", expanded=False):
            st.json(
                {
                    "intent": latest_audit["intent"],
                    "presentation": latest_audit["presentation"],
                    "evidence": latest_audit["evidence"],
                }
            )


def main() -> None:
    if not DB_PATH.exists():
        st.error("Database not found. Run the ingest and scenario generation scripts first.")
        return

    with closing(get_conn()) as conn:
        scenarios = _load_scenarios(conn)
        if scenarios.empty:
            st.error("No scenarios found. Run `uv run python generate_demo_context.py` first.")
            return

        scenario_labels = _scenario_options(scenarios)
        selected_scenario = st.sidebar.selectbox(
            "Scenario",
            scenarios["scenario_id"].tolist(),
            format_func=lambda value: scenario_labels[value],
        )
        scenario_row = scenarios[scenarios["scenario_id"] == selected_scenario].iloc[0]

        planner = PricingDecisionService(conn)
        conversation = PricingConversationService(planner)
        plan = planner.build_plan_bundle(selected_scenario)

        st.sidebar.caption(f"Profile: {scenario_row['profile_id'] or 'demo'}")
        st.sidebar.caption(f"Budget limit: {_format_pct(float(scenario_row['budget_pct']))}")
        st.sidebar.caption(f"Safety stock: {_format_pct(float(scenario_row['safety_stock_pct']))}")
        st.sidebar.markdown("The official proposal is fixed. Every what-if runs on a separate child solve.")
        with st.sidebar.expander("Scenario story", expanded=True):
            st.write(_scenario_explainer(selected_scenario))
        with st.sidebar.expander("What this app can do", expanded=False):
            st.markdown(
                """
                - explain the official proposal
                - inspect one product decision
                - answer why or why not for one allowed discount
                - run one bounded what-if without changing the official proposal
                """
            )

        _render_hero(plan)
        stage = st.radio(
            "Workflow",
            _stage_options(),
            horizontal=True,
            label_visibility="collapsed",
            key=f"stage_{selected_scenario}",
        )

        if stage == "1. Decision Brief":
            _render_decision_brief(plan)
        elif stage == "2. Product Decisions":
            _render_product_decisions(plan, planner)
        elif stage == "3. Ask & Simulate":
            _render_ask_and_simulate(plan, conversation)
        else:
            _render_method_and_audit(plan)


if __name__ == "__main__":
    main()
