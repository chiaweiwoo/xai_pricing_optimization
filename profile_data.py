"""Create data profiling outputs for the pricing demo."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from ingest import DATASET_VERSION_ID
from xai_pricing.config import REPORTS_DIR
from xai_pricing.db import get_conn

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:  # pragma: no cover
    plt = None


def _latest_quality_checks(conn) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT check_name, severity, passed, details_json
        FROM quality_checks
        WHERE dataset_version_id = ?
        ORDER BY quality_check_id
        """,
        conn,
        params=(DATASET_VERSION_ID,),
    )


def _save_line_chart(df: pd.DataFrame, x: str, y: str, title: str, path: Path) -> None:
    if plt is None:
        return
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(df[x], df[y], linewidth=1.6)
    ax.set_title(title)
    ax.set_xlabel("")
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _save_multi_line_chart(df: pd.DataFrame, title: str, path: Path) -> None:
    if plt is None:
        return
    fig, ax = plt.subplots(figsize=(10, 4))
    for column in df.columns[1:]:
        ax.plot(df.iloc[:, 0], df[column], linewidth=1.4, label=column)
    ax.set_title(title)
    ax.grid(alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        sales = pd.read_sql(
            """
            SELECT *
            FROM v_valid_weekly_sales
            WHERE dataset_version_id = ?
            """,
            conn,
            params=(DATASET_VERSION_ID,),
            parse_dates=["week_end_date"],
        )
        products = pd.read_sql("SELECT * FROM products", conn)
        stores = pd.read_sql("SELECT * FROM stores", conn)
        chain = pd.read_sql(
            """
            SELECT *
            FROM v_chain_sku_week
            WHERE dataset_version_id = ?
            ORDER BY week_end_date
            """,
            conn,
            params=(DATASET_VERSION_ID,),
            parse_dates=["week_end_date"],
        )
        quality_checks = _latest_quality_checks(conn)
    finally:
        conn.close()

    merged = sales.merge(products, on="upc", how="left").merge(stores, on="store_id", how="left")

    weekly_totals = (
        sales.groupby("week_end_date", as_index=False)[["units", "spend", "visits", "households"]]
        .sum()
        .sort_values("week_end_date")
    )
    category_weekly = (
        merged.groupby(["week_end_date", "category"], as_index=False)["units"]
        .sum()
        .pivot(index="week_end_date", columns="category", values="units")
        .fillna(0.0)
        .reset_index()
    )
    top_categories = (
        merged.groupby("category", as_index=False)["spend"].sum()
        .sort_values("spend", ascending=False)
        .head(4)["category"]
        .tolist()
    )
    category_weekly = category_weekly[["week_end_date"] + [col for col in top_categories if col in category_weekly.columns]]
    state_summary = (
        merged.groupby("state_prov_code", as_index=False)[["units", "spend"]]
        .sum()
        .sort_values("spend", ascending=False)
    )

    discount_rows = merged[merged["price"].notna() & merged["base_price"].notna() & (merged["base_price"] > 0)].copy()
    discount_rows["discount_rate"] = (discount_rows["base_price"] - discount_rows["price"]) / discount_rows["base_price"]

    recent_2009 = weekly_totals[
        (weekly_totals["week_end_date"] >= "2009-09-01") & (weekly_totals["week_end_date"] <= "2009-12-31")
    ]["units"].mean()
    stable_2010 = weekly_totals[
        (weekly_totals["week_end_date"] >= "2010-01-01") & (weekly_totals["week_end_date"] <= "2010-12-31")
    ]["units"].mean()
    late_2011 = weekly_totals[
        (weekly_totals["week_end_date"] >= "2011-08-01") & (weekly_totals["week_end_date"] <= "2011-10-31")
    ]["units"].mean()

    summary = {
        "dataset_version_id": DATASET_VERSION_ID,
        "date_range": {
            "start": sales["week_end_date"].min().date().isoformat(),
            "end": sales["week_end_date"].max().date().isoformat(),
        },
        "counts": {
            "sales_rows": int(len(sales)),
            "weeks": int(sales["week_end_date"].nunique()),
            "stores": int(sales["store_id"].nunique()),
            "products": int(sales["upc"].nunique()),
        },
        "measures": {
            "total_units": float(sales["units"].sum()),
            "total_spend": float(sales["spend"].sum()),
            "avg_price": float(sales["price"].mean()),
            "avg_base_price": float(sales["base_price"].mean()),
            "promo_feature_rate": float(sales["feature"].mean()),
            "promo_display_rate": float(sales["display"].mean()),
            "promo_tpr_rate": float(sales["tpr_only"].mean()),
        },
        "medium_article_check": {
            "avg_units_sep_dec_2009": float(recent_2009),
            "avg_units_2010": float(stable_2010),
            "avg_units_aug_oct_2011": float(late_2011),
        },
        "sufficiency": {
            "observed_enough_for_history": True,
            "needs_synthetic_cost_inventory_competitor": True,
            "needs_external_signals_for_v1": False,
        },
    }

    (REPORTS_DIR / "data_profile_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    _save_line_chart(
        weekly_totals,
        "week_end_date",
        "units",
        "Weekly Chain Units",
        REPORTS_DIR / "weekly_chain_units.png",
    )
    _save_line_chart(
        weekly_totals,
        "week_end_date",
        "spend",
        "Weekly Chain Spend",
        REPORTS_DIR / "weekly_chain_spend.png",
    )
    if len(category_weekly.columns) > 1:
        _save_multi_line_chart(
            category_weekly,
            "Top Category Weekly Units",
            REPORTS_DIR / "category_weekly_units.png",
        )

    quality_lines = []
    for _, row in quality_checks.iterrows():
        quality_lines.append(
            f"- `{row['check_name']}`: {'pass' if row['passed'] else 'flag'} ({row['severity']}) {row['details_json']}"
        )

    category_table = (
        merged.groupby("category", as_index=False)
        .agg(units=("units", "sum"), spend=("spend", "sum"), avg_price=("price", "mean"))
        .sort_values("spend", ascending=False)
        .head(8)
    )
    state_table = state_summary.head(8)

    markdown = f"""# Data Profile

## Dataset summary

- Dataset version: `{DATASET_VERSION_ID}`
- Date range: `{summary['date_range']['start']}` to `{summary['date_range']['end']}`
- Rows / weeks / stores / products: `{summary['counts']['sales_rows']:,}` / `{summary['counts']['weeks']}` / `{summary['counts']['stores']}` / `{summary['counts']['products']}`
- Total units / spend: `{summary['measures']['total_units']:.1f}` / `${summary['measures']['total_spend']:.2f}`
- Mean price / mean base price: `${summary['measures']['avg_price']:.3f}` / `${summary['measures']['avg_base_price']:.3f}`

## Data quality

{chr(10).join(quality_lines) if quality_lines else '- No quality checks recorded yet.'}

## Medium article hypothesis check

- Average weekly units in Sep-Dec 2009: `{summary['medium_article_check']['avg_units_sep_dec_2009']:.1f}`
- Average weekly units in 2010: `{summary['medium_article_check']['avg_units_2010']:.1f}`
- Average weekly units in Aug-Oct 2011: `{summary['medium_article_check']['avg_units_aug_oct_2011']:.1f}`
- Interpretation: the article is useful as an EDA checklist, but the optimizer should not treat these aggregate patterns as causal demand truths.

## Top categories

```
{category_table.to_string(index=False)}
```

## Top states by spend

```
{state_table.to_string(index=False)}
```

## External signals and context sufficiency

- For this v1 pricing optimizer, public history is enough to anchor product assortment, price ranges, promo context, and realistic volume scale.
- Weather, macro, and search-trend features are not required yet because we are explicitly skipping demand forecasting.
- The missing decision-critical inputs are cost, inventory, competitor price, and price elasticity. Those are generated deterministically in `generate_demo_context.py` and stored as `origin='synthetic'`.
"""

    (REPORTS_DIR / "data_profile.md").write_text(markdown, encoding="utf-8")
    print(f"Wrote {REPORTS_DIR / 'data_profile.md'}")
    print(f"Wrote {REPORTS_DIR / 'data_profile_summary.json'}")


if __name__ == "__main__":
    main()
