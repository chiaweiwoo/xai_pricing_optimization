import hashlib
import random
import sqlite3
from dataclasses import dataclass

import pandas as pd

from .config import (
    DEFAULT_SCENARIO_ID,
    DEFAULT_SCENARIO_NAME,
    DEFAULT_SYNTHETIC_SEED,
)
from .db import json_dumps, utc_now
from .demand import ConstantElasticityDemandModel


DISCOUNT_STEPS = [0.00, 0.05, 0.10, 0.15, 0.20, 0.25]
DEFAULT_BUDGET_PCT = 0.10
DEFAULT_SAFETY_STOCK_PCT = 0.25
COMPETITOR_TOLERANCE_PCT = 0.05

PROFILE_CONFIG = {
    "balanced_campaign_v1": {
        "inventory_scale": 1.0,
        "inbound_scale": 1.0,
        "scenario_name": "Balanced Promotion Campaign v1",
    },
    "inventory_stress_v1": {
        "inventory_scale": 0.62,
        "inbound_scale": 0.70,
        "scenario_name": "Inventory Stress Campaign v1",
    },
}


@dataclass(frozen=True)
class ScenarioSettings:
    scenario_id: str = DEFAULT_SCENARIO_ID
    scenario_name: str = DEFAULT_SCENARIO_NAME
    seed: int = DEFAULT_SYNTHETIC_SEED
    competitor_name: str = "MarketSquare"
    profile_id: str = "balanced_campaign_v1"
    budget_pct: float = DEFAULT_BUDGET_PCT
    safety_stock_pct: float = DEFAULT_SAFETY_STOCK_PCT


def _stable_seed(seed: int, key: str) -> int:
    digest = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _round_money(value: float) -> float:
    return round(value + 1e-9, 2)


def _round_units(value: float) -> float:
    return round(value + 1e-9, 4)


def _role_from_rank(rank: int, total: int) -> str:
    pct = rank / max(total, 1)
    if pct <= 0.15:
        return "kvi"
    if pct <= 0.40:
        return "traffic_driver"
    if pct <= 0.70:
        return "margin_driver"
    return "long_tail"


def _pick_archetype(
    *,
    category: str,
    sub_category: str,
    role: str,
    ordinal: int,
) -> str:
    category = category.lower()
    sub_category = sub_category.lower()
    if "pizza" in sub_category:
        return "low_inventory" if ordinal % 2 == 0 else "promotion_opportunity"
    if "pretzel" in sub_category:
        return "overstock"
    if "cereal" in category:
        return "competitor_pressure" if ordinal % 3 else "promotion_opportunity"
    if role == "margin_driver":
        return "margin_constrained"
    if role == "traffic_driver":
        return "promotion_opportunity" if ordinal % 2 else "competitor_pressure"
    if ordinal % 5 == 0:
        return "low_inventory"
    return "neutral"


def _archetype_recipe(
    *,
    archetype: str,
    role: str,
    profile_id: str,
    rng: random.Random,
) -> dict[str, float]:
    inventory_scale = PROFILE_CONFIG[profile_id]["inventory_scale"]
    inbound_scale = PROFILE_CONFIG[profile_id]["inbound_scale"]

    if archetype == "competitor_pressure":
        return {
            "elasticity": rng.uniform(-2.6, -1.9),
            "cost_ratio": rng.uniform(0.62, 0.72),
            "weeks_of_cover": rng.uniform(2.7, 4.2) * inventory_scale,
            "inbound_weeks": rng.uniform(0.7, 1.2) * inbound_scale,
            "competitor_multiplier": rng.uniform(0.82, 0.90),
            "min_margin_pct": 0.20 if role in {"kvi", "traffic_driver"} else 0.24,
            "competitor_tolerance_pct": 0.05,
            "competitor_weight": 3 if role == "kvi" else 2,
        }
    if archetype == "low_inventory":
        return {
            "elasticity": rng.uniform(-1.8, -1.1),
            "cost_ratio": rng.uniform(0.58, 0.70),
            "weeks_of_cover": rng.uniform(1.1, 1.8) * inventory_scale,
            "inbound_weeks": rng.uniform(0.1, 0.5) * inbound_scale,
            "competitor_multiplier": rng.uniform(0.92, 0.99),
            "min_margin_pct": 0.22,
            "competitor_tolerance_pct": 0.06,
            "competitor_weight": 2 if role in {"kvi", "traffic_driver"} else 1,
        }
    if archetype == "overstock":
        return {
            "elasticity": rng.uniform(-2.4, -1.6),
            "cost_ratio": rng.uniform(0.52, 0.64),
            "weeks_of_cover": rng.uniform(7.0, 10.0) * inventory_scale,
            "inbound_weeks": rng.uniform(0.8, 1.5) * inbound_scale,
            "competitor_multiplier": rng.uniform(0.74, 0.84),
            "min_margin_pct": 0.20,
            "competitor_tolerance_pct": 0.05,
            "competitor_weight": 2 if role in {"kvi", "traffic_driver"} else 1,
        }
    if archetype == "margin_constrained":
        return {
            "elasticity": rng.uniform(-1.3, -0.8),
            "cost_ratio": rng.uniform(0.72, 0.83),
            "weeks_of_cover": rng.uniform(3.2, 5.0) * inventory_scale,
            "inbound_weeks": rng.uniform(0.4, 0.9) * inbound_scale,
            "competitor_multiplier": rng.uniform(0.90, 0.98),
            "min_margin_pct": 0.28,
            "competitor_tolerance_pct": 0.05,
            "competitor_weight": 1,
        }
    if archetype == "promotion_opportunity":
        return {
            "elasticity": rng.uniform(-2.2, -1.5),
            "cost_ratio": rng.uniform(0.50, 0.62),
            "weeks_of_cover": rng.uniform(4.5, 7.0) * inventory_scale,
            "inbound_weeks": rng.uniform(0.6, 1.1) * inbound_scale,
            "competitor_multiplier": rng.uniform(0.80, 0.90),
            "min_margin_pct": 0.20 if role in {"kvi", "traffic_driver"} else 0.24,
            "competitor_tolerance_pct": 0.05,
            "competitor_weight": 2 if role in {"kvi", "traffic_driver"} else 1,
        }
    return {
        "elasticity": rng.uniform(-1.4, -0.9),
        "cost_ratio": rng.uniform(0.56, 0.68),
        "weeks_of_cover": rng.uniform(2.8, 4.2) * inventory_scale,
        "inbound_weeks": rng.uniform(0.4, 0.8) * inbound_scale,
        "competitor_multiplier": rng.uniform(0.98, 1.05),
        "min_margin_pct": 0.22 if role in {"kvi", "traffic_driver"} else 0.26,
        "competitor_tolerance_pct": 0.05,
        "competitor_weight": 1,
    }


def build_demo_scenario(
    conn: sqlite3.Connection,
    dataset_version_id: str,
    settings: ScenarioSettings | None = None,
) -> dict[str, int | float | str]:
    settings = settings or ScenarioSettings()
    if settings.profile_id not in PROFILE_CONFIG:
        raise ValueError(f"Unknown profile_id: {settings.profile_id}")

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

    latest_week = product_df["week_end_date"].dropna().max()
    trailing_window = latest_week - pd.Timedelta(weeks=13)
    recent_df = product_df[product_df["week_end_date"] >= trailing_window].copy()

    revenue_rank_df = (
        recent_df.groupby("upc", as_index=False)["spend"].sum().sort_values("spend", ascending=False)
    )
    revenue_rank = {str(upc): idx + 1 for idx, upc in enumerate(revenue_rank_df["upc"])}
    total_products = revenue_rank_df["upc"].nunique()

    grouped: list[dict[str, object]] = []
    for ordinal, (upc, sku_df) in enumerate(product_df.groupby("upc", sort=True), start=1):
        rng = random.Random(_stable_seed(settings.seed, f"{settings.profile_id}:{upc}"))
        recent_sku = recent_df[recent_df["upc"] == upc].copy()
        effective_sku = recent_sku if not recent_sku.empty else sku_df.copy()
        nonpromo = effective_sku[
            (effective_sku["feature"] == 0)
            & (effective_sku["display"] == 0)
            & (effective_sku["tpr_only"] == 0)
            & effective_sku["price"].notna()
            & effective_sku["units"].notna()
        ]
        baseline_source = nonpromo if not nonpromo.empty else effective_sku[effective_sku["units"].notna()]
        if baseline_source.empty:
            continue

        list_price_source = nonpromo["base_price"].dropna()
        if list_price_source.empty:
            list_price_source = effective_sku["base_price"].dropna()
        if list_price_source.empty:
            list_price_source = effective_sku["price"].dropna()
        if list_price_source.empty:
            continue

        baseline_units = float(max(baseline_source["units"].median(), 1.0))
        list_price = _round_money(float(list_price_source.median()))
        role = _role_from_rank(revenue_rank.get(str(upc), total_products), total_products)
        category = str(sku_df["category"].iloc[0])
        sub_category = str(sku_df["sub_category"].iloc[0])
        archetype = _pick_archetype(
            category=category,
            sub_category=sub_category,
            role=role,
            ordinal=ordinal,
        )
        recipe = _archetype_recipe(
            archetype=archetype,
            role=role,
            profile_id=settings.profile_id,
            rng=rng,
        )

        grouped.append(
            _repair_row(
                {
                    "upc": str(upc),
                    "description": sku_df["description"].iloc[0],
                    "manufacturer": sku_df["manufacturer"].iloc[0],
                    "category": category,
                    "sub_category": sub_category,
                    "product_size": sku_df["product_size"].iloc[0],
                    "role": role,
                    "archetype": archetype,
                    "list_price": list_price,
                    "current_price": list_price,
                    "reference_price": list_price,
                    "baseline_units": baseline_units,
                    "elasticity": round(recipe["elasticity"], 4),
                    "unit_cost": _round_money(list_price * recipe["cost_ratio"]),
                    "weeks_of_cover": round(recipe["weeks_of_cover"], 2),
                    "on_hand_units": int(round(baseline_units * recipe["weeks_of_cover"])),
                    "inbound_units": int(round(baseline_units * recipe["inbound_weeks"])),
                    "competitor_price": _round_money(list_price * recipe["competitor_multiplier"]),
                    "uncertainty_pct": 0.12 if role == "kvi" else 0.16 if role == "traffic_driver" else 0.10,
                    "min_margin_pct": recipe["min_margin_pct"],
                    "max_discount_pct": max(DISCOUNT_STEPS),
                    "competitor_tolerance_pct": recipe["competitor_tolerance_pct"],
                    "competitor_weight": recipe["competitor_weight"],
                    "safety_stock_units": round(baseline_units * settings.safety_stock_pct, 2),
                }
            )
        )

    scenario_df = pd.DataFrame(grouped).sort_values("upc").reset_index(drop=True)
    stats = _persist_scenario(conn, dataset_version_id, latest_week.strftime("%Y-%m-%d"), scenario_df, settings)
    return {
        "profile_id": settings.profile_id,
        "products": int(len(scenario_df)),
        "candidate_outcomes": int(stats["candidate_count"]),
        "hard_valid_candidates": int(stats["hard_valid_candidates"]),
        "promotable_products": int(stats["promotable_products"]),
        "planning_week_end": latest_week.strftime("%Y-%m-%d"),
    }


def _repair_row(row: dict[str, object]) -> dict[str, object]:
    attempt = 0
    while attempt < 6:
        candidate_rows = _evaluate_candidates(row)
        if any(candidate["is_hard_valid"] for candidate in candidate_rows):
            row["candidate_rows"] = candidate_rows
            return row

        min_margin_pct = float(row["min_margin_pct"])
        list_price = float(row["list_price"])
        unit_cost_cap = list_price * (1 - min_margin_pct - 0.03)
        row["unit_cost"] = _round_money(min(float(row["unit_cost"]), unit_cost_cap))

        zero_discount = next(candidate for candidate in candidate_rows if candidate["discount_pct"] == 0.0)
        if "inventory" in zero_discount["hard_violation_reason"]:
            baseline_units = float(row["baseline_units"])
            safety_stock = float(row["safety_stock_units"])
            required_inventory = baseline_units + safety_stock
            total_inventory = max(float(row["on_hand_units"]) + float(row["inbound_units"]), required_inventory)
            row["on_hand_units"] = int(round(total_inventory * 0.8))
            row["inbound_units"] = int(round(total_inventory - int(row["on_hand_units"])))
        attempt += 1

    baseline_units = float(row["baseline_units"])
    uncertainty_pct = float(row["uncertainty_pct"])
    safety_stock = float(row["safety_stock_units"])
    min_margin_pct = float(row["min_margin_pct"])
    list_price = float(row["list_price"])
    row["unit_cost"] = _round_money(min(float(row["unit_cost"]), list_price * (1 - min_margin_pct - 0.05)))
    minimum_total_inventory = baseline_units * (1 + uncertainty_pct) + safety_stock + 1
    row["on_hand_units"] = int(round(minimum_total_inventory * 0.8))
    row["inbound_units"] = int(round(minimum_total_inventory - int(row["on_hand_units"])))
    row["candidate_rows"] = _evaluate_candidates(row)
    return row


def _evaluate_candidates(row: dict[str, object]) -> list[dict[str, object]]:
    model = ConstantElasticityDemandModel(
        reference_price=float(row["reference_price"]),
        baseline_units=float(row["baseline_units"]),
        elasticity=float(row["elasticity"]),
        uncertainty_pct=float(row["uncertainty_pct"]),
    )
    total_inventory = int(row["on_hand_units"]) + int(row["inbound_units"])
    safety_stock_units = float(row["safety_stock_units"])
    min_margin_pct = float(row["min_margin_pct"])
    competitor_price = float(row["competitor_price"])
    competitor_tolerance = float(row["competitor_tolerance_pct"])
    list_price = float(row["list_price"])
    unit_cost = float(row["unit_cost"])

    candidate_rows: list[dict[str, object]] = []
    for rank, discount_pct in enumerate(DISCOUNT_STEPS, start=1):
        candidate_price = _round_money(list_price * (1 - discount_pct))
        estimate = model.predict(candidate_price)
        expected_units_capped = min(estimate.mean_units, total_inventory)
        optimistic_units_capped = min(estimate.upper_units, total_inventory)
        expected_lost_units = max(estimate.mean_units - total_inventory, 0.0)
        optimistic_lost_units = max(estimate.upper_units - total_inventory, 0.0)
        ending_inventory = total_inventory - optimistic_units_capped
        gross_margin_pct = (candidate_price - unit_cost) / candidate_price if candidate_price else 0.0
        violation_reasons: list[str] = []
        if gross_margin_pct + 1e-9 < min_margin_pct:
            violation_reasons.append("margin")
        if ending_inventory + 1e-9 < safety_stock_units:
            violation_reasons.append("inventory")
        competitor_index = candidate_price / competitor_price if competitor_price else None
        competitor_gap = (
            max(0.0, competitor_index - (1 + competitor_tolerance))
            if competitor_index is not None
            else 0.0
        )
        revenue = expected_units_capped * candidate_price
        gross_profit = expected_units_capped * (candidate_price - unit_cost)

        candidate_rows.append(
            {
                "candidate_rank": rank,
                "candidate_price": candidate_price,
                "discount_pct": round(discount_pct, 4),
                "expected_units": _round_units(estimate.mean_units),
                "conservative_units": _round_units(estimate.lower_units),
                "optimistic_units": _round_units(estimate.upper_units),
                "revenue": round(revenue, 4),
                "gross_profit": round(gross_profit, 4),
                "gross_margin_pct": round(gross_margin_pct, 4),
                "competitor_index": round(competitor_index, 4) if competitor_index is not None else None,
                "inventory_cap_units": total_inventory,
                "expected_units_capped": _round_units(expected_units_capped),
                "list_price": list_price,
                "markdown_investment": round(expected_units_capped * (list_price - candidate_price), 4),
                "ending_inventory_units": _round_units(ending_inventory),
                "expected_lost_units": _round_units(expected_lost_units),
                "optimistic_lost_units": _round_units(optimistic_lost_units),
                "competitor_gap": round(competitor_gap, 4),
                "is_hard_valid": int(not violation_reasons),
                "hard_violation_reason": ",".join(violation_reasons) if violation_reasons else None,
                "is_current_price": int(abs(candidate_price - float(row["current_price"])) < 0.001),
                "is_reference_price": int(abs(candidate_price - float(row["reference_price"])) < 0.001),
            }
        )
    return candidate_rows


def _persist_scenario(
    conn: sqlite3.Connection,
    dataset_version_id: str,
    planning_week_end: str,
    scenario_df: pd.DataFrame,
    settings: ScenarioSettings,
) -> dict[str, int]:
    candidate_count = 0
    hard_valid_candidates = 0
    promotable_products = 0

    existing_runs = [
        row[0]
        for row in conn.execute(
            "SELECT run_id FROM optimizer_runs WHERE scenario_id = ?",
            (settings.scenario_id,),
        ).fetchall()
    ]
    for run_id in existing_runs:
        conn.execute("DELETE FROM optimizer_run_items WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM optimizer_run_phases WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM optimizer_runs WHERE run_id = ?", (run_id,))

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
            created_at,
            scenario_kind,
            parent_scenario_id,
            profile_id,
            budget_pct,
            safety_stock_pct
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            settings.scenario_id,
            settings.scenario_name,
            dataset_version_id,
            planning_week_end,
            settings.seed,
            "lexicographic_competitor_then_profit",
            json_dumps(
                {
                    "source": "synthetic_context",
                    "scope": "pricing_optimization_demo",
                    "competitor_name": settings.competitor_name,
                    "discount_steps": DISCOUNT_STEPS,
                }
            ),
            utc_now(),
            "official",
            None,
            settings.profile_id,
            settings.budget_pct,
            settings.safety_stock_pct,
        ),
    )

    for _, row in scenario_df.iterrows():
        conn.execute(
            """
            INSERT INTO product_context (
                scenario_id, upc, strategic_role, current_price, reference_price,
                unit_cost, min_margin_pct, max_discount_pct, role_rank, origin,
                archetype, competitor_tolerance_pct, competitor_weight, list_price
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.scenario_id,
                row["upc"],
                row["role"],
                row["current_price"],
                row["reference_price"],
                row["unit_cost"],
                row["min_margin_pct"],
                row["max_discount_pct"],
                {"kvi": 1, "traffic_driver": 2, "margin_driver": 3, "long_tail": 4}[row["role"]],
                "synthetic",
                row["archetype"],
                row["competitor_tolerance_pct"],
                row["competitor_weight"],
                row["list_price"],
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
                sell_through_target_pct, origin, safety_stock_units
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.scenario_id,
                row["upc"],
                row["on_hand_units"],
                row["inbound_units"],
                row["weeks_of_cover"],
                0.85 if row["archetype"] == "overstock" else 0.70,
                "synthetic",
                row["safety_stock_units"],
            ),
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

        sku_has_valid_promo = False
        for candidate in row["candidate_rows"]:
            conn.execute(
                """
                INSERT INTO candidate_outcomes (
                    scenario_id, upc, candidate_rank, candidate_price, discount_pct,
                    expected_units, conservative_units, optimistic_units, revenue,
                    gross_profit, gross_margin_pct, competitor_index, inventory_cap_units,
                    expected_units_capped, is_current_price, is_reference_price, origin,
                    list_price, markdown_investment, ending_inventory_units,
                    expected_lost_units, optimistic_lost_units, competitor_gap,
                    is_hard_valid, hard_violation_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settings.scenario_id,
                    row["upc"],
                    candidate["candidate_rank"],
                    candidate["candidate_price"],
                    candidate["discount_pct"],
                    candidate["expected_units"],
                    candidate["conservative_units"],
                    candidate["optimistic_units"],
                    candidate["revenue"],
                    candidate["gross_profit"],
                    candidate["gross_margin_pct"],
                    candidate["competitor_index"],
                    candidate["inventory_cap_units"],
                    candidate["expected_units_capped"],
                    candidate["is_current_price"],
                    candidate["is_reference_price"],
                    "synthetic",
                    candidate["list_price"],
                    candidate["markdown_investment"],
                    candidate["ending_inventory_units"],
                    candidate["expected_lost_units"],
                    candidate["optimistic_lost_units"],
                    candidate["competitor_gap"],
                    candidate["is_hard_valid"],
                    candidate["hard_violation_reason"],
                ),
            )
            candidate_count += 1
            hard_valid_candidates += int(candidate["is_hard_valid"])
            if candidate["is_hard_valid"] and candidate["discount_pct"] > 0:
                sku_has_valid_promo = True
        promotable_products += int(sku_has_valid_promo)

    relationship_rows = []
    for (_, category), cat_df in scenario_df.groupby(["manufacturer", "category"]):
        ordered = cat_df.sort_values("list_price")
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
    return {
        "candidate_count": candidate_count,
        "hard_valid_candidates": hard_valid_candidates,
        "promotable_products": promotable_products,
    }
