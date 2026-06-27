import hashlib
import random
import sqlite3
from dataclasses import dataclass

import pandas as pd

from .config import DEFAULT_SCENARIO_ID, DEFAULT_SCENARIO_NAME, DEFAULT_SYNTHETIC_SEED
from .db import json_dumps, utc_now
from .demand import ConstantElasticityDemandModel


DISCOUNT_STEPS = [-0.05, 0.00, 0.05, 0.10, 0.15, 0.20, 0.25]


@dataclass(frozen=True)
class ScenarioSettings:
    scenario_id: str = DEFAULT_SCENARIO_ID
    scenario_name: str = DEFAULT_SCENARIO_NAME
    seed: int = DEFAULT_SYNTHETIC_SEED
    competitor_name: str = "MarketSquare"


def _stable_seed(seed: int, upc: str) -> int:
    digest = hashlib.sha256(f"{seed}:{upc}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _snap_price(candidate_price: float, historical_prices: list[float]) -> float:
    if not historical_prices:
        return round(candidate_price, 2)
    return min(historical_prices, key=lambda price: (abs(price - candidate_price), price))


def _role_from_rank(rank: int, total: int) -> str:
    pct = rank / max(total, 1)
    if pct <= 0.15:
        return "kvi"
    if pct <= 0.40:
        return "traffic_driver"
    if pct <= 0.70:
        return "margin_driver"
    return "long_tail"


def build_demo_scenario(
    conn: sqlite3.Connection,
    dataset_version_id: str,
    settings: ScenarioSettings | None = None,
) -> dict[str, int]:
    settings = settings or ScenarioSettings()
    product_df = pd.read_sql(
        """
        SELECT
            chain.upc,
            p.description,
            p.manufacturer,
            p.category,
            p.sub_category,
            p.product_size,
            chain.week_end_date,
            chain.units,
            chain.visits,
            chain.households,
            chain.spend,
            chain.price,
            chain.base_price,
            chain.feature,
            chain.display,
            chain.tpr_only
        FROM v_chain_sku_week chain
        JOIN products p ON p.upc = chain.upc
        WHERE chain.dataset_version_id = ?
        ORDER BY chain.upc, chain.week_end_date
        """,
        conn,
        params=(dataset_version_id,),
        parse_dates=["week_end_date"],
    )
    if product_df.empty:
        raise RuntimeError("No chain-level data found. Run ingest.py first.")

    latest_week = (
        product_df["week_end_date"].dropna().max().strftime("%Y-%m-%d")
    )
    trailing_window = product_df["week_end_date"].dropna().max() - pd.Timedelta(weeks=26)
    recent_df = product_df[product_df["week_end_date"] >= trailing_window].copy()

    grouped = []
    revenue_rank_df = (
        recent_df.groupby("upc", as_index=False)["spend"].sum().sort_values("spend", ascending=False)
    )
    revenue_rank = {str(upc): idx + 1 for idx, upc in enumerate(revenue_rank_df["upc"])}
    total_products = revenue_rank_df["upc"].nunique()

    for upc, sku_df in product_df.groupby("upc", sort=True):
        rng = random.Random(_stable_seed(settings.seed, str(upc)))
        recent_sku = recent_df[recent_df["upc"] == upc].copy()
        effective_sku = recent_sku if not recent_sku.empty else sku_df.copy()
        nonpromo = recent_sku[
            (recent_sku["feature"] == 0)
            & (recent_sku["display"] == 0)
            & (recent_sku["tpr_only"] == 0)
            & recent_sku["price"].notna()
            & recent_sku["units"].notna()
        ]
        if nonpromo.empty:
            nonpromo = sku_df[
                (sku_df["feature"] == 0)
                & (sku_df["display"] == 0)
                & (sku_df["tpr_only"] == 0)
                & sku_df["price"].notna()
                & sku_df["units"].notna()
            ]
        baseline_source = nonpromo if not nonpromo.empty else effective_sku[effective_sku["units"].notna()]
        baseline_units = float(baseline_source["units"].median())
        current_price_source = effective_sku["price"].dropna()
        reference_price_source = effective_sku["base_price"].dropna()
        if current_price_source.empty or reference_price_source.empty:
            current_price_source = sku_df["price"].dropna()
            reference_price_source = sku_df["base_price"].dropna()
        if current_price_source.empty or reference_price_source.empty:
            continue
        current_price = float(current_price_source.iloc[-1])
        reference_price = float(reference_price_source.median())
        role_rank = revenue_rank.get(str(upc), total_products)
        role = _role_from_rank(role_rank, total_products)
        category = str(sku_df["category"].iloc[0]).lower()
        sub_category = str(sku_df["sub_category"].iloc[0]).lower()

        if "cereal" in category:
            elasticity = rng.uniform(-2.8, -1.8)
        elif "pizza" in sub_category:
            elasticity = rng.uniform(-1.3, -0.7)
        elif role == "margin_driver":
            elasticity = rng.uniform(-1.1, -0.6)
        elif role == "traffic_driver":
            elasticity = rng.uniform(-2.0, -1.3)
        else:
            elasticity = rng.uniform(-1.8, -0.9)

        margin_ratio = {
            "kvi": rng.uniform(0.72, 0.82),
            "traffic_driver": rng.uniform(0.68, 0.78),
            "margin_driver": rng.uniform(0.48, 0.62),
            "long_tail": rng.uniform(0.58, 0.72),
        }[role]
        unit_cost = round(current_price * margin_ratio, 2)

        if "pretzel" in sub_category:
            weeks_of_cover = rng.uniform(8.0, 11.0)
        elif "pizza" in sub_category:
            weeks_of_cover = rng.uniform(1.6, 2.4)
        else:
            weeks_of_cover = rng.uniform(3.0, 7.0)
        on_hand_units = int(round(baseline_units * weeks_of_cover))
        inbound_units = int(round(max(baseline_units * rng.uniform(0.3, 1.2), 0)))

        if "cereal" in category:
            competitor_price = round(current_price * rng.uniform(0.92, 0.98), 2)
        elif "pretzel" in sub_category:
            competitor_price = round(current_price * rng.uniform(1.03, 1.10), 2)
        elif "pizza" in sub_category:
            competitor_price = round(current_price * rng.uniform(0.96, 1.00), 2)
        else:
            competitor_price = round(current_price * rng.uniform(0.95, 1.06), 2)

        uncertainty_pct = {
            "kvi": 0.12,
            "traffic_driver": 0.15,
            "margin_driver": 0.10,
            "long_tail": 0.20,
        }[role]

        grouped.append(
            {
                "upc": str(upc),
                "description": sku_df["description"].iloc[0],
                "manufacturer": sku_df["manufacturer"].iloc[0],
                "category": sku_df["category"].iloc[0],
                "sub_category": sku_df["sub_category"].iloc[0],
                "product_size": sku_df["product_size"].iloc[0],
                "role": role,
                "current_price": current_price,
                "reference_price": reference_price,
                "baseline_units": baseline_units,
                "elasticity": round(elasticity, 4),
                "unit_cost": unit_cost,
                "weeks_of_cover": round(weeks_of_cover, 2),
                "on_hand_units": on_hand_units,
                "inbound_units": inbound_units,
                "competitor_price": competitor_price,
                "uncertainty_pct": uncertainty_pct,
                "historical_prices": sorted(
                    {round(value, 2) for value in sku_df["price"].dropna().tolist()}
                ),
            }
        )

    scenario_df = pd.DataFrame(grouped).sort_values("upc").reset_index(drop=True)
    candidate_outcomes = _persist_scenario(conn, dataset_version_id, latest_week, scenario_df, settings)
    return {
        "products": int(len(scenario_df)),
        "candidate_outcomes": int(candidate_outcomes),
        "planning_week_end": latest_week,
    }


def _persist_scenario(
    conn: sqlite3.Connection,
    dataset_version_id: str,
    planning_week_end: str,
    scenario_df: pd.DataFrame,
    settings: ScenarioSettings,
) -> int:
    candidate_count = 0
    conn.execute("DELETE FROM candidate_outcomes WHERE scenario_id = ?", (settings.scenario_id,))
    conn.execute("DELETE FROM demand_models WHERE scenario_id = ?", (settings.scenario_id,))
    conn.execute("DELETE FROM competitor_prices WHERE scenario_id = ?", (settings.scenario_id,))
    conn.execute("DELETE FROM inventory_positions WHERE scenario_id = ?", (settings.scenario_id,))
    conn.execute("DELETE FROM product_relationships WHERE scenario_id = ?", (settings.scenario_id,))
    conn.execute("DELETE FROM product_context WHERE scenario_id = ?", (settings.scenario_id,))
    conn.execute("DELETE FROM scenarios WHERE scenario_id = ?", (settings.scenario_id,))

    conn.execute(
        """
        INSERT INTO scenarios (
            scenario_id,
            scenario_name,
            dataset_version_id,
            planning_week_end,
            seed,
            objective,
            notes_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            settings.scenario_id,
            settings.scenario_name,
            dataset_version_id,
            planning_week_end,
            settings.seed,
            "gross_profit",
            json_dumps(
                {
                    "source": "synthetic_context",
                    "scope": "pricing_optimization_demo",
                    "competitor_name": settings.competitor_name,
                }
            ),
            utc_now(),
        ),
    )

    for _, row in scenario_df.iterrows():
        conn.execute(
            """
            INSERT INTO product_context (
                scenario_id, upc, strategic_role, current_price, reference_price,
                unit_cost, min_margin_pct, max_discount_pct, role_rank, origin
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.scenario_id,
                row["upc"],
                row["role"],
                row["current_price"],
                row["reference_price"],
                row["unit_cost"],
                0.20 if row["role"] in {"kvi", "traffic_driver"} else 0.28,
                0.25,
                {"kvi": 1, "traffic_driver": 2, "margin_driver": 3, "long_tail": 4}[row["role"]],
                "synthetic",
            ),
        )
        conn.execute(
            """
            INSERT INTO competitor_prices (
                scenario_id, upc, competitor_name, competitor_price, competitor_index, origin
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                settings.scenario_id,
                row["upc"],
                settings.competitor_name,
                row["competitor_price"],
                row["current_price"] / row["competitor_price"] if row["competitor_price"] else None,
                "synthetic",
            ),
        )
        conn.execute(
            """
            INSERT INTO inventory_positions (
                scenario_id, upc, on_hand_units, inbound_units, weeks_of_cover,
                sell_through_target_pct, origin
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.scenario_id,
                row["upc"],
                row["on_hand_units"],
                row["inbound_units"],
                row["weeks_of_cover"],
                0.85 if row["weeks_of_cover"] > 6 else 0.70,
                "synthetic",
            ),
        )

        model = ConstantElasticityDemandModel(
            reference_price=row["reference_price"],
            baseline_units=row["baseline_units"],
            elasticity=row["elasticity"],
            uncertainty_pct=row["uncertainty_pct"],
        )
        conn.execute(
            """
            INSERT INTO demand_models (
                scenario_id, upc, model_type, reference_price, baseline_units,
                elasticity, uncertainty_pct, params_json, origin
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.scenario_id,
                row["upc"],
                "constant_elasticity",
                row["reference_price"],
                row["baseline_units"],
                row["elasticity"],
                row["uncertainty_pct"],
                json_dumps(
                    {
                        "reference_price": row["reference_price"],
                        "baseline_units": row["baseline_units"],
                        "elasticity": row["elasticity"],
                        "uncertainty_pct": row["uncertainty_pct"],
                    }
                ),
                "synthetic",
            ),
        )

        for rank, discount_pct in enumerate(DISCOUNT_STEPS, start=1):
            target_price = row["current_price"] * (1 - discount_pct)
            snapped_price = _snap_price(target_price, row["historical_prices"])
            if snapped_price <= row["unit_cost"]:
                continue
            estimate = model.predict(snapped_price)
            inventory_cap = row["on_hand_units"] + row["inbound_units"]
            capped_units = min(estimate.mean_units, inventory_cap)
            gross_profit = capped_units * (snapped_price - row["unit_cost"])
            revenue = capped_units * snapped_price
            conn.execute(
                """
                INSERT INTO candidate_outcomes (
                    scenario_id, upc, candidate_rank, candidate_price, discount_pct,
                    expected_units, conservative_units, optimistic_units, revenue,
                    gross_profit, gross_margin_pct, competitor_index, inventory_cap_units,
                    expected_units_capped, is_current_price, is_reference_price, origin
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settings.scenario_id,
                    row["upc"],
                    rank,
                    round(snapped_price, 2),
                    round(discount_pct, 4),
                    round(estimate.mean_units, 4),
                    round(estimate.lower_units, 4),
                    round(estimate.upper_units, 4),
                    round(revenue, 4),
                    round(gross_profit, 4),
                    round((snapped_price - row["unit_cost"]) / snapped_price, 4),
                    round(snapped_price / row["competitor_price"], 4) if row["competitor_price"] else None,
                    int(inventory_cap),
                    round(capped_units, 4),
                    int(abs(snapped_price - row["current_price"]) < 0.001),
                    int(abs(snapped_price - row["reference_price"]) < 0.001),
                    "synthetic",
                ),
            )
            candidate_count += 1

    relationship_rows = []
    for (_, category), cat_df in scenario_df.groupby(["manufacturer", "category"]):
        ordered = cat_df.sort_values("current_price")
        if len(ordered) < 2:
            continue
        for relation_order, (_, row) in enumerate(ordered.iterrows(), start=1):
            relationship_rows.append(
                (
                    settings.scenario_id,
                    f"{row['manufacturer']}::{row['category']}",
                    "price_ladder",
                    row["upc"],
                    relation_order,
                    "synthetic",
                )
            )
    conn.executemany(
        """
        INSERT INTO product_relationships (
            scenario_id, relationship_group_id, relationship_type, upc, relation_order, origin
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        relationship_rows,
    )
    conn.commit()
    return candidate_count
