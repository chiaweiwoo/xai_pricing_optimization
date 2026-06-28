from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any

import pandas as pd

from .db import json_dumps
from .optimizer import PhaseResult, PricingOptimizer, SolveRequest, SolveResult


OBJECTIVE_ORDER_LABELS = {
    "official": (
        "Minimize weighted competitor gap",
        "Maximize gross profit within that competitor outcome",
        "Prefer shallower discounts when the earlier phases tie",
    ),
    "profit_first": (
        "Maximize gross profit",
        "Minimize weighted competitor gap within that profit outcome",
        "Prefer shallower discounts when the earlier phases tie",
    ),
}


@dataclass(frozen=True)
class BenchmarkResult:
    benchmark_id: str
    label: str
    status: str
    summary: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PlanBundle:
    scenario_id: str
    scenario: dict[str, Any]
    official: SolveResult
    profit_first: SolveResult
    current_price: BenchmarkResult
    theoretical_ceiling: BenchmarkResult
    brief: dict[str, Any]
    catalog: list[dict[str, Any]]


@dataclass(frozen=True)
class CounterfactualResult:
    source_run_id: str
    result: SolveResult
    comparison: dict[str, Any]
    cached: bool


class PricingDecisionService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.optimizer = PricingOptimizer(conn)

    def build_plan_bundle(self, scenario_id: str) -> PlanBundle:
        scenario = self.load_scenario_context(scenario_id)
        official = self._enrich_result(
            self.ensure_solver_run(
                SolveRequest(
                    scenario_id=scenario_id,
                    run_id=f"{scenario_id}__official",
                    run_kind="official",
                    objective_mode="official",
                )
            )
        )
        profit_first = self._enrich_result(
            self.ensure_solver_run(
                SolveRequest(
                    scenario_id=scenario_id,
                    run_id=f"{scenario_id}__benchmark_profit_first",
                    run_kind="benchmark",
                    objective_mode="profit_first",
                )
            )
        )
        current_price = self.build_current_price_benchmark(scenario_id)
        theoretical_ceiling = self.build_theoretical_ceiling_benchmark(scenario_id)
        catalog = self.load_product_catalog(scenario_id)
        brief = self._build_plan_brief(
            scenario=scenario,
            official=official,
            profit_first=profit_first,
            current_price=current_price,
            theoretical_ceiling=theoretical_ceiling,
        )
        return PlanBundle(
            scenario_id=scenario_id,
            scenario=scenario,
            official=official,
            profit_first=profit_first,
            current_price=current_price,
            theoretical_ceiling=theoretical_ceiling,
            brief=brief,
            catalog=catalog,
        )

    def ensure_solver_run(self, request: SolveRequest) -> SolveResult:
        if request.run_id is not None:
            existing = self._load_run_row(request.run_id)
            if existing is not None:
                return self.load_run_result(request.run_id)
        return self.optimizer.solve(request)

    def load_run_result(self, run_id: str) -> SolveResult:
        run_row = self._load_run_row(run_id)
        if run_row is None:
            raise RuntimeError(f"Run not found: {run_id}")
        diagnostics = json.loads(run_row["diagnostics_json"])
        phases = [
            PhaseResult(
                phase_name=row["phase_name"],
                status=row["status"],
                objective_value=row["objective_value"],
                duration_ms=int(row["duration_ms"]),
                details=json.loads(row["details_json"]),
            )
            for row in self.conn.execute(
                """
                SELECT phase_name, status, objective_value, duration_ms, details_json
                FROM optimizer_run_phases
                WHERE run_id = ?
                ORDER BY phase_name
                """,
                (run_id,),
            ).fetchall()
        ]
        selections = [
            self._deserialize_selection_row(row)
            for row in self.conn.execute(
                """
                SELECT
                    upc, candidate_rank, candidate_price, discount_pct, gross_profit,
                    expected_units, ending_inventory_units, competitor_gap, evidence_json
                FROM optimizer_run_items
                WHERE run_id = ?
                ORDER BY selection_rank, upc
                """,
                (run_id,),
            ).fetchall()
        ]
        summary = diagnostics.get("summary", {"selected_products": len(selections)})
        result = SolveResult(
            run_id=run_id,
            scenario_id=run_row["scenario_id"],
            status=run_row["status"],
            summary=summary,
            phases=phases,
            diagnostics=diagnostics,
            selections=selections,
        )
        return self._enrich_result(result)

    def load_scenario_context(self, scenario_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
                scenario_id,
                scenario_name,
                profile_id,
                budget_pct,
                safety_stock_pct,
                planning_week_end,
                objective,
                notes_json
            FROM scenarios
            WHERE scenario_id = ?
            """,
            (scenario_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Scenario not found: {scenario_id}")
        notes = json.loads(row["notes_json"]) if row["notes_json"] else {}
        return {
            "scenario_id": row["scenario_id"],
            "scenario_name": row["scenario_name"],
            "profile_id": row["profile_id"],
            "budget_pct": float(row["budget_pct"]),
            "safety_stock_pct": float(row["safety_stock_pct"]),
            "planning_week_end": row["planning_week_end"],
            "objective": row["objective"],
            "notes": notes,
        }

    def load_product_catalog(self, scenario_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                p.scenario_id,
                p.upc,
                prod.description,
                prod.manufacturer,
                prod.category,
                prod.sub_category,
                prod.product_size,
                p.strategic_role,
                p.archetype,
                p.current_price,
                p.reference_price,
                p.unit_cost,
                p.min_margin_pct,
                p.max_discount_pct,
                p.competitor_tolerance_pct,
                p.competitor_weight,
                comp.competitor_price,
                comp.competitor_index,
                d.baseline_units,
                i.on_hand_units,
                i.inbound_units,
                i.safety_stock_units
            FROM product_context p
            JOIN products prod
              ON prod.upc = p.upc
            JOIN demand_models d
              ON d.scenario_id = p.scenario_id
             AND d.upc = p.upc
            JOIN inventory_positions i
              ON i.scenario_id = p.scenario_id
             AND i.upc = p.upc
            LEFT JOIN competitor_prices comp
              ON comp.scenario_id = p.scenario_id
             AND comp.upc = p.upc
            WHERE p.scenario_id = ?
            ORDER BY prod.category, prod.sub_category, prod.description, p.upc
            """,
            (scenario_id,),
        ).fetchall()
        catalog: list[dict[str, Any]] = []
        for row in rows:
            catalog.append(
                {
                    "scenario_id": row["scenario_id"],
                    "upc": row["upc"],
                    "description": row["description"],
                    "manufacturer": row["manufacturer"],
                    "category": row["category"],
                    "sub_category": row["sub_category"],
                    "product_size": row["product_size"],
                    "product_label": self._product_label(
                        row["description"],
                        row["product_size"],
                        row["upc"],
                    ),
                    "role": row["strategic_role"],
                    "archetype": row["archetype"],
                    "current_price": round(float(row["current_price"]), 2),
                    "reference_price": round(float(row["reference_price"]), 2),
                    "unit_cost": round(float(row["unit_cost"]), 4),
                    "min_margin_pct": round(float(row["min_margin_pct"]), 4),
                    "max_discount_pct": round(float(row["max_discount_pct"]), 4),
                    "competitor_tolerance_pct": round(float(row["competitor_tolerance_pct"]), 4),
                    "competitor_weight": int(row["competitor_weight"]),
                    "competitor_price": round(float(row["competitor_price"]), 2)
                    if row["competitor_price"] is not None
                    else None,
                    "competitor_index_current": round(float(row["competitor_index"]), 4)
                    if row["competitor_index"] is not None
                    else None,
                    "baseline_units": round(float(row["baseline_units"]), 4),
                    "on_hand_units": round(float(row["on_hand_units"]), 4),
                    "inbound_units": round(float(row["inbound_units"]), 4),
                    "safety_stock_units": round(float(row["safety_stock_units"]), 4),
                }
            )
        return catalog

    def get_allowed_discount_buckets(self, scenario_id: str, upc: str | None = None) -> list[float]:
        if upc is None:
            rows = self.conn.execute(
                """
                SELECT DISTINCT discount_pct
                FROM candidate_outcomes
                WHERE scenario_id = ?
                ORDER BY discount_pct
                """,
                (scenario_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT DISTINCT discount_pct
                FROM candidate_outcomes
                WHERE scenario_id = ? AND upc = ?
                ORDER BY discount_pct
                """,
                (scenario_id, upc),
            ).fetchall()
        return [round(float(row["discount_pct"]), 4) for row in rows]

    def build_current_price_benchmark(self, scenario_id: str) -> BenchmarkResult:
        working_df, request, scenario = self._build_working_frame(scenario_id)
        current_df = (
            working_df[working_df["is_current_price"] == 1]
            .sort_values(["upc", "candidate_rank"])
            .groupby("upc", as_index=False)
            .head(1)
            .copy()
        )
        invalid_rows = current_df[~current_df["effective_hard_valid"]]
        status = "ready" if invalid_rows.empty else "violations_detected"
        summary = self.optimizer._build_summary(current_df, float(scenario["budget_pct"]))
        metadata = {
            "objective_mode": request.objective_mode,
            "invalid_skus": invalid_rows["upc"].tolist(),
            "hard_violation_examples": self.optimizer._summarize_violation_examples(current_df),
        }
        return BenchmarkResult(
            benchmark_id="current_price",
            label="Current-price baseline",
            status=status,
            summary=summary,
            metadata=metadata,
        )

    def build_theoretical_ceiling_benchmark(self, scenario_id: str) -> BenchmarkResult:
        working_df, request, scenario = self._build_working_frame(scenario_id)
        ceiling_df = (
            working_df[working_df["effective_hard_valid"]]
            .sort_values(["upc", "gross_profit", "discount_pct"], ascending=[True, False, True])
            .groupby("upc", as_index=False)
            .head(1)
            .copy()
        )
        summary = self.optimizer._build_summary(ceiling_df, float(scenario["budget_pct"]))
        respects_budget = summary["budget_utilization_pct"] <= float(scenario["budget_pct"]) + 1e-9
        metadata = {
            "objective_mode": request.objective_mode,
            "respects_budget": respects_budget,
            "budget_gap_pct": round(summary["budget_utilization_pct"] - float(scenario["budget_pct"]), 4),
            "ignored_portfolio_rules": [
                "portfolio_budget",
                "competitor_priority",
            ],
        }
        return BenchmarkResult(
            benchmark_id="theoretical_ceiling",
            label="Theoretical profit ceiling",
            status="analytic_ceiling",
            summary=summary,
            metadata=metadata,
        )

    def get_sku_dossier(self, run_id: str, upc: str) -> dict[str, Any]:
        run_row = self._load_run_row(run_id)
        if run_row is None:
            raise RuntimeError(f"Run not found: {run_id}")
        request = self._request_from_run_row(run_row)
        working_df, _, _ = self._build_working_frame(
            run_row["scenario_id"],
            request=request,
        )
        sku_df = working_df[working_df["upc"] == upc].copy()
        if sku_df.empty:
            raise RuntimeError(f"UPC not found in scenario {run_row['scenario_id']}: {upc}")

        selected_row = self._load_selected_candidate_row(run_id, upc, sku_df)
        current_row = sku_df[sku_df["is_current_price"] == 1].iloc[0]
        local_best_row = (
            sku_df[sku_df["effective_hard_valid"]]
            .sort_values(["gross_profit", "discount_pct"], ascending=[False, True])
            .iloc[0]
        )

        alternatives: list[dict[str, Any]] = []
        for row in sku_df.sort_values("discount_pct").itertuples(index=False):
            alternatives.append(
                {
                    "discount_pct": round(float(row.discount_pct), 4),
                    "candidate_price": round(float(row.candidate_price), 2),
                    "gross_profit": round(float(row.gross_profit), 4),
                    "expected_units": round(float(row.expected_units_capped), 4),
                    "revenue": round(float(row.revenue), 4),
                    "ending_inventory_units": round(float(row.ending_inventory_units), 4),
                    "gross_margin_pct": round(float(row.gross_margin_pct), 4),
                    "competitor_index": round(float(row.competitor_index), 4)
                    if row.competitor_index is not None
                    else None,
                    "competitor_gap": round(float(row.effective_competitor_gap), 4),
                    "effective_hard_valid": bool(row.effective_hard_valid),
                    "reason": row.effective_hard_violation_reason,
                    "is_selected": int(row.candidate_rank) == int(selected_row["candidate_rank"]),
                    "is_current": bool(row.is_current_price),
                }
            )

        product = self._catalog_map(run_row["scenario_id"]).get(upc, {"upc": upc, "product_label": upc})
        selected_evidence = self._row_to_evidence(selected_row)
        current_evidence = self._row_to_evidence(current_row)
        local_best_evidence = self._row_to_evidence(local_best_row)
        return {
            "run_id": run_id,
            "scenario_id": run_row["scenario_id"],
            "upc": upc,
            "product": product,
            "objective_mode": run_row["objective_mode"],
            "available_discount_buckets": self.get_allowed_discount_buckets(run_row["scenario_id"], upc),
            "selected": selected_evidence,
            "current": current_evidence,
            "local_best_feasible": local_best_evidence,
            "selected_vs_current": self._metric_delta(selected_evidence, current_evidence),
            "selected_vs_local_best": self._metric_delta(selected_evidence, local_best_evidence),
            "context": {
                "current_price": product.get("current_price"),
                "reference_price": product.get("reference_price"),
                "unit_cost": product.get("unit_cost"),
                "baseline_units": product.get("baseline_units"),
                "competitor_price": product.get("competitor_price"),
                "competitor_index_current": product.get("competitor_index_current"),
                "competitor_tolerance_pct": product.get("competitor_tolerance_pct"),
                "competitor_weight": product.get("competitor_weight"),
                "on_hand_units": product.get("on_hand_units"),
                "inbound_units": product.get("inbound_units"),
                "safety_stock_units": product.get("safety_stock_units"),
            },
            "alternatives": alternatives,
        }

    def simulate_counterfactual(
        self,
        source_run_id: str,
        *,
        exact_discount_locks: dict[str, float] | None = None,
        budget_pct: float | None = None,
        safety_stock_pct: float | None = None,
        min_margin_overrides: dict[str, float] | None = None,
        competitor_tolerance_overrides: dict[str, float] | None = None,
    ) -> CounterfactualResult:
        source_row = self._load_run_row(source_run_id)
        if source_row is None:
            raise RuntimeError(f"Source run not found: {source_run_id}")
        if source_row["status"] != "optimal":
            raise RuntimeError(f"Source run must be optimal for counterfactuals: {source_run_id}")

        child_request = SolveRequest(
            scenario_id=source_row["scenario_id"],
            run_id=self._counterfactual_run_id(
                source_run_id=source_run_id,
                exact_discount_locks=exact_discount_locks or {},
                budget_pct=budget_pct,
                safety_stock_pct=safety_stock_pct,
                min_margin_overrides=min_margin_overrides or {},
                competitor_tolerance_overrides=competitor_tolerance_overrides or {},
                objective_mode=source_row["objective_mode"],
            ),
            run_kind="what_if",
            source_run_id=source_run_id,
            objective_mode=source_row["objective_mode"],
            budget_pct=budget_pct,
            safety_stock_pct=safety_stock_pct,
            min_margin_overrides=min_margin_overrides or {},
            competitor_tolerance_overrides=competitor_tolerance_overrides or {},
            exact_discount_locks=exact_discount_locks or {},
        )
        cached = self._load_run_row(child_request.run_id) is not None
        result = self.ensure_solver_run(child_request)
        comparison = self.compare_runs(source_run_id, result.run_id)
        return CounterfactualResult(
            source_run_id=source_run_id,
            result=result,
            comparison=comparison,
            cached=cached,
        )

    def compare_runs(self, base_run_id: str, candidate_run_id: str) -> dict[str, Any]:
        base = self.load_run_result(base_run_id)
        candidate = self.load_run_result(candidate_run_id)
        comparison: dict[str, Any] = {
            "base_run_id": base_run_id,
            "candidate_run_id": candidate_run_id,
            "base_status": base.status,
            "candidate_status": candidate.status,
            "comparable": candidate.status == "optimal",
        }
        if candidate.status != "optimal":
            comparison["changed_sku_count"] = 0
            comparison["summary_delta"] = None
            comparison["changed_skus"] = []
            comparison["infeasibility"] = self._summarize_infeasibility(candidate)
            return comparison

        metrics = [
            "selected_products",
            "promoted_products",
            "protected_products",
            "total_revenue",
            "total_gross_profit",
            "total_markdown_investment",
            "budget_utilization_pct",
            "weighted_competitor_gap",
            "inventory_tight_products",
        ]
        deltas = {
            metric: round(float(candidate.summary.get(metric, 0)) - float(base.summary.get(metric, 0)), 4)
            for metric in metrics
        }

        base_items = pd.read_sql(
            """
            SELECT upc, candidate_price, discount_pct, gross_profit, expected_units, competitor_gap
            FROM optimizer_run_items
            WHERE run_id = ?
            """,
            self.conn,
            params=(base_run_id,),
        )
        candidate_items = pd.read_sql(
            """
            SELECT upc, candidate_price, discount_pct, gross_profit, expected_units, competitor_gap
            FROM optimizer_run_items
            WHERE run_id = ?
            """,
            self.conn,
            params=(candidate_run_id,),
        )
        merged = base_items.merge(candidate_items, on="upc", suffixes=("_base", "_candidate"))
        changed = merged[
            (merged["discount_pct_base"].sub(merged["discount_pct_candidate"]).abs() > 1e-9)
            | (merged["candidate_price_base"].sub(merged["candidate_price_candidate"]).abs() > 1e-9)
        ].copy()
        changed["gross_profit_delta"] = changed["gross_profit_candidate"] - changed["gross_profit_base"]
        changed["expected_units_delta"] = changed["expected_units_candidate"] - changed["expected_units_base"]
        changed["competitor_gap_delta"] = changed["competitor_gap_candidate"] - changed["competitor_gap_base"]

        changed_rows: list[dict[str, Any]] = []
        product_map = self._catalog_map(base.scenario_id)
        for row in changed.sort_values("gross_profit_delta").itertuples(index=False):
            product = product_map.get(row.upc, {})
            changed_rows.append(
                {
                    "upc": row.upc,
                    "product_label": product.get("product_label", row.upc),
                    "description": product.get("description"),
                    "category": product.get("category"),
                    "discount_pct_base": round(float(row.discount_pct_base), 4),
                    "discount_pct_candidate": round(float(row.discount_pct_candidate), 4),
                    "gross_profit_delta": round(float(row.gross_profit_delta), 4),
                    "expected_units_delta": round(float(row.expected_units_delta), 4),
                    "competitor_gap_delta": round(float(row.competitor_gap_delta), 4),
                }
            )

        comparison["changed_sku_count"] = int(len(changed_rows))
        comparison["summary_delta"] = deltas
        comparison["changed_skus"] = changed_rows
        comparison["infeasibility"] = None
        return comparison

    def _build_working_frame(
        self,
        scenario_id: str,
        *,
        request: SolveRequest | None = None,
    ) -> tuple[pd.DataFrame, SolveRequest, sqlite3.Row]:
        scenario = self.optimizer._load_scenario(scenario_id)
        effective_request = request or SolveRequest(scenario_id=scenario_id, objective_mode="official")
        candidate_df = self.optimizer._load_candidate_frame(scenario_id)
        working_df, _ = self.optimizer._prepare_candidates(
            candidate_df,
            effective_budget_pct=(
                float(effective_request.budget_pct)
                if effective_request.budget_pct is not None
                else float(scenario["budget_pct"])
            ),
            effective_safety_stock_pct=(
                float(effective_request.safety_stock_pct)
                if effective_request.safety_stock_pct is not None
                else float(scenario["safety_stock_pct"])
            ),
            request=effective_request,
        )
        return working_df, effective_request, scenario

    def _load_run_row(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT
                run_id,
                scenario_id,
                source_run_id,
                run_kind,
                status,
                solver_name,
                objective_mode,
                input_hash,
                budget_pct,
                safety_stock_pct,
                diagnostics_json,
                created_at,
                completed_at
            FROM optimizer_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    def _request_from_run_row(self, run_row: sqlite3.Row) -> SolveRequest:
        diagnostics = json.loads(run_row["diagnostics_json"])
        rule_inputs = diagnostics.get("rule_inputs", {})
        return SolveRequest(
            scenario_id=run_row["scenario_id"],
            run_id=run_row["run_id"],
            run_kind=run_row["run_kind"],
            source_run_id=run_row["source_run_id"],
            objective_mode=run_row["objective_mode"],
            budget_pct=float(run_row["budget_pct"]),
            safety_stock_pct=float(run_row["safety_stock_pct"]),
            min_margin_overrides=rule_inputs.get("min_margin_overrides", {}),
            competitor_tolerance_overrides=rule_inputs.get("competitor_tolerance_overrides", {}),
            exact_discount_locks=rule_inputs.get("exact_discount_locks", {}),
        )

    def _load_selected_candidate_row(self, run_id: str, upc: str, sku_df: pd.DataFrame) -> pd.Series:
        row = self.conn.execute(
            """
            SELECT candidate_rank
            FROM optimizer_run_items
            WHERE run_id = ? AND upc = ?
            """,
            (run_id, upc),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Selected SKU not found in run {run_id}: {upc}")
        selected = sku_df[sku_df["candidate_rank"] == int(row["candidate_rank"])]
        return selected.iloc[0]

    def _row_to_evidence(self, row: pd.Series) -> dict[str, Any]:
        return {
            "candidate_rank": int(row["candidate_rank"]),
            "discount_pct": round(float(row["discount_pct"]), 4),
            "candidate_price": round(float(row["candidate_price"]), 2),
            "gross_profit": round(float(row["gross_profit"]), 4),
            "expected_units": round(float(row["expected_units_capped"]), 4),
            "revenue": round(float(row["revenue"]), 4),
            "ending_inventory_units": round(float(row["ending_inventory_units"]), 4),
            "inventory_buffer_units": round(float(row["inventory_buffer_units"]), 4),
            "gross_margin_pct": round(float(row["gross_margin_pct"]), 4),
            "competitor_index": round(float(row["competitor_index"]), 4)
            if row["competitor_index"] is not None
            else None,
            "competitor_gap": round(float(row["effective_competitor_gap"]), 4),
            "effective_hard_valid": bool(row["effective_hard_valid"]),
            "reason": row["effective_hard_violation_reason"],
        }

    def _catalog_map(self, scenario_id: str) -> dict[str, dict[str, Any]]:
        return {row["upc"]: row for row in self.load_product_catalog(scenario_id)}

    def _enrich_result(self, result: SolveResult) -> SolveResult:
        if not result.selections:
            return result
        product_map = self._catalog_map(result.scenario_id)
        selections: list[dict[str, Any]] = []
        for row in result.selections:
            upc = str(row["upc"])
            product = product_map.get(upc, {})
            selections.append(
                {
                    **row,
                    "description": row.get("description") or product.get("description"),
                    "manufacturer": row.get("manufacturer") or product.get("manufacturer"),
                    "category": row.get("category") or product.get("category"),
                    "sub_category": row.get("sub_category") or product.get("sub_category"),
                    "product_size": row.get("product_size") or product.get("product_size"),
                    "product_label": row.get("product_label")
                    or product.get("product_label")
                    or self._product_label(
                        product.get("description"),
                        product.get("product_size"),
                        upc,
                    ),
                }
            )
        return SolveResult(
            run_id=result.run_id,
            scenario_id=result.scenario_id,
            status=result.status,
            summary=result.summary,
            phases=result.phases,
            diagnostics=result.diagnostics,
            selections=selections,
        )

    def _metric_delta(self, candidate: dict[str, Any], base: dict[str, Any]) -> dict[str, float]:
        metrics = [
            "candidate_price",
            "gross_profit",
            "expected_units",
            "revenue",
            "ending_inventory_units",
            "inventory_buffer_units",
            "competitor_gap",
        ]
        return {
            metric: round(float(candidate.get(metric, 0.0)) - float(base.get(metric, 0.0)), 4)
            for metric in metrics
        }

    def _build_plan_brief(
        self,
        *,
        scenario: dict[str, Any],
        official: SolveResult,
        profit_first: SolveResult,
        current_price: BenchmarkResult,
        theoretical_ceiling: BenchmarkResult,
    ) -> dict[str, Any]:
        official_summary = official.summary
        current_summary = current_price.summary
        profit_first_summary = profit_first.summary
        gp_vs_current = round(
            float(official_summary["total_gross_profit"]) - float(current_summary["total_gross_profit"]),
            2,
        )
        revenue_vs_current = round(
            float(official_summary["total_revenue"]) - float(current_summary["total_revenue"]),
            2,
        )
        gap_improvement_vs_current = round(
            float(current_summary["weighted_competitor_gap"]) - float(official_summary["weighted_competitor_gap"]),
            4,
        )
        gp_vs_profit_first = round(
            float(official_summary["total_gross_profit"]) - float(profit_first_summary["total_gross_profit"]),
            2,
        )
        gap_improvement_vs_profit_first = round(
            float(profit_first_summary["weighted_competitor_gap"]) - float(official_summary["weighted_competitor_gap"]),
            4,
        )
        budget_limit = float(scenario["budget_pct"])
        budget_used = float(official_summary["budget_utilization_pct"])
        budget_binding = budget_used >= max(budget_limit - 0.0025, 0.0)
        strategy = (
            "price_position_strategy"
            if gap_improvement_vs_current > 1.0 and gp_vs_profit_first < 0
            else "balanced_profit_strategy"
        )
        status = "review_required" if gp_vs_current < 0 else "on_track"
        headline = (
            f"Protect price position on {int(official_summary['promoted_products'])} promoted SKUs "
            f"while accepting a {abs(gp_vs_current):,.0f} gross-profit trade-off."
            if strategy == "price_position_strategy"
            else f"Improve revenue and competitor position with {int(official_summary['promoted_products'])} promoted SKUs."
        )
        return {
            "status": status,
            "strategy": strategy,
            "headline": headline,
            "decision": "Set one discrete discount for each SKU for the next pricing cycle.",
            "objective_order": list(OBJECTIVE_ORDER_LABELS.get(official.diagnostics.get("objective_mode", "official"), ())),
            "tradeoff_summary": (
                "The official proposal is not the highest-profit feasible plan. "
                "It minimizes weighted competitor gap first, then maximizes gross profit, then avoids extra discount depth."
            ),
            "profit_vs_current": gp_vs_current,
            "revenue_vs_current": revenue_vs_current,
            "gap_improvement_vs_current": gap_improvement_vs_current,
            "profit_vs_profit_first": gp_vs_profit_first,
            "gap_improvement_vs_profit_first": gap_improvement_vs_profit_first,
            "budget_binding": budget_binding,
            "budget_limit_pct": budget_limit,
            "budget_used_pct": budget_used,
            "inventory_tight_products": int(official_summary["inventory_tight_products"]),
            "promoted_products": int(official_summary["promoted_products"]),
            "protected_products": int(official_summary["protected_products"]),
            "selected_products": int(official_summary["selected_products"]),
            "theoretical_headroom_gp": round(
                float(theoretical_ceiling.summary["total_gross_profit"]) - float(official_summary["total_gross_profit"]),
                2,
            ),
            "next_actions": [
                "Review the promoted and protected SKU mix before approving the campaign.",
                "Inspect inventory-tight products to see where stock protection limited discount depth.",
                "Use Ask & Simulate to test one override or one rule change without changing the official proposal.",
            ],
        }

    def _product_label(self, description: str | None, product_size: str | None, upc: str) -> str:
        if description and product_size:
            return f"{description} ({product_size})"
        if description:
            return description
        return upc

    def _counterfactual_run_id(
        self,
        *,
        source_run_id: str,
        exact_discount_locks: dict[str, float],
        budget_pct: float | None,
        safety_stock_pct: float | None,
        min_margin_overrides: dict[str, float],
        competitor_tolerance_overrides: dict[str, float],
        objective_mode: str,
    ) -> str:
        payload = {
            "source_run_id": source_run_id,
            "objective_mode": objective_mode,
            "budget_pct": budget_pct,
            "safety_stock_pct": safety_stock_pct,
            "exact_discount_locks": exact_discount_locks,
            "min_margin_overrides": min_margin_overrides,
            "competitor_tolerance_overrides": competitor_tolerance_overrides,
        }
        digest = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()[:10]
        return f"{source_run_id}__what_if__{digest}"

    def _summarize_infeasibility(self, result: SolveResult) -> dict[str, Any]:
        precheck = result.diagnostics.get("precheck", {})
        return {
            "status": result.status,
            "invalid_skus": precheck.get("invalid_skus", []),
            "lock_conflicts": precheck.get("lock_conflicts", []),
            "hard_violation_counts": precheck.get("hard_violation_counts", {}),
            "hard_violation_examples": precheck.get("hard_violation_examples", []),
        }

    def _deserialize_selection_row(self, row: sqlite3.Row) -> dict[str, Any]:
        evidence = json.loads(row["evidence_json"])
        return {
            "upc": row["upc"],
            "candidate_rank": int(row["candidate_rank"]),
            "candidate_price": round(float(row["candidate_price"]), 2),
            "discount_pct": round(float(row["discount_pct"]), 4),
            "gross_profit": round(float(row["gross_profit"]), 4),
            "expected_units": round(float(row["expected_units"]), 4),
            "ending_inventory_units": round(float(row["ending_inventory_units"]), 4),
            "competitor_gap": round(float(row["competitor_gap"]), 4),
            "archetype": evidence.get("archetype"),
            "role": evidence.get("strategic_role"),
            **evidence,
        }
