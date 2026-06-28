from __future__ import annotations

from typing import Any


def build_selection_snapshot(
    official_selections: list[dict[str, Any]],
    current_selections: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_map = {row["upc"]: row for row in current_selections}
    catalog_map = {row["upc"]: row for row in catalog}
    snapshot: list[dict[str, Any]] = []
    for row in official_selections:
        upc = row["upc"]
        current = current_map.get(upc, {})
        product = catalog_map.get(upc, {})
        ending_inventory = float(row.get("ending_inventory_units", 0.0) or 0.0)
        safety_stock = float(product.get("safety_stock_units", 0.0) or 0.0)
        inbound_units = float(product.get("inbound_units", 0.0) or 0.0)
        on_hand_units = float(product.get("on_hand_units", 0.0) or 0.0)
        inventory_buffer = ending_inventory - safety_stock
        inventory_cover = on_hand_units + inbound_units - safety_stock
        snapshot.append(
            {
                "upc": upc,
                "product_label": row.get("product_label") or product.get("product_label") or upc,
                "category": product.get("category"),
                "role": row.get("role") or product.get("role"),
                "archetype": row.get("archetype") or product.get("archetype"),
                "discount_pct": float(row.get("discount_pct", 0.0) or 0.0),
                "current_discount_pct": float(current.get("discount_pct", 0.0) or 0.0),
                "gross_profit": float(row.get("gross_profit", 0.0) or 0.0),
                "current_gross_profit": float(current.get("gross_profit", 0.0) or 0.0),
                "competitor_gap": float(row.get("competitor_gap", 0.0) or 0.0),
                "current_competitor_gap": float(current.get("competitor_gap", 0.0) or 0.0),
                "ending_inventory_units": ending_inventory,
                "safety_stock_units": safety_stock,
                "inventory_buffer_units": inventory_buffer,
                "inventory_cover_units": inventory_cover,
                "competitor_gap_improvement": float(current.get("competitor_gap", 0.0) or 0.0)
                - float(row.get("competitor_gap", 0.0) or 0.0),
                "gross_profit_tradeoff": float(row.get("gross_profit", 0.0) or 0.0)
                - float(current.get("gross_profit", 0.0) or 0.0),
            }
        )
    return snapshot


def select_review_cases(snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not snapshot:
        return []

    competitor_case = max(
        snapshot,
        key=lambda row: (
            row["competitor_gap_improvement"],
            row["discount_pct"],
            -row["gross_profit_tradeoff"],
            row["product_label"],
        ),
    )

    inventory_case = min(
        snapshot,
        key=lambda row: (
            row["inventory_buffer_units"],
            row["discount_pct"],
            row["gross_profit_tradeoff"],
            row["product_label"],
        ),
    )

    tradeoff_case = min(
        snapshot,
        key=lambda row: (
            row["gross_profit_tradeoff"],
            -row["competitor_gap_improvement"],
            row["product_label"],
        ),
    )

    ordered = [
        (
            "competitor_response",
            "Strongest competitor response",
            "This product is where the recommendation improves price position the most versus current pricing.",
            competitor_case,
        ),
        (
            "inventory_protection",
            "Inventory-protected product",
            "This product ends closest to its safety-stock buffer, so the recommendation stays careful here.",
            inventory_case,
        ),
        (
            "gross_profit_tradeoff",
            "Largest gross-profit trade-off",
            "This product gives up the most gross profit versus current pricing to support the broader campaign story.",
            tradeoff_case,
        ),
    ]

    cases: list[dict[str, Any]] = []
    used_upcs: set[str] = set()
    fallback_pool = sorted(
        snapshot,
        key=lambda row: (
            -row["competitor_gap_improvement"],
            row["inventory_buffer_units"],
            row["gross_profit_tradeoff"],
            row["product_label"],
        ),
    )

    for case_id, title, description, chosen in ordered:
        candidate = chosen
        if candidate["upc"] in used_upcs:
            candidate = next((row for row in fallback_pool if row["upc"] not in used_upcs), chosen)
        used_upcs.add(candidate["upc"])
        cases.append(
            {
                "case_id": case_id,
                "title": title,
                "description": description,
                "upc": candidate["upc"],
                "product_label": candidate["product_label"],
                "role": candidate["role"],
                "discount_pct": candidate["discount_pct"],
                "current_discount_pct": candidate["current_discount_pct"],
                "gross_profit_tradeoff": candidate["gross_profit_tradeoff"],
                "competitor_gap_improvement": candidate["competitor_gap_improvement"],
                "inventory_buffer_units": candidate["inventory_buffer_units"],
            }
        )
    return cases
