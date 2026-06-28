from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any

import pandas as pd

from .db import json_dumps
from .optimizer import PhaseResult, PricingOptimizer, SolveRequest, SolveResult


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
    official: SolveResult
    profit_first: SolveResult
    current_price: BenchmarkResult
    theoretical_ceiling: BenchmarkResult


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
        official = self.ensure_solver_run(
            SolveRequest(
                scenario_id=scenario_id,
                run_id=f"{scenario_id}__official",
                run_kind="official",
                objective_mode="official",
            )
        )
        profit_first = self.ensure_solver_run(
            SolveRequest(
                scenario_id=scenario_id,
                run_id=f"{scenario_id}__benchmark_profit_first",
                run_kind="benchmark",
                objective_mode="profit_first",
            )
        )
        current_price = self.build_current_price_benchmark(scenario_id)
        theoretical_ceiling = self.build_theoretical_ceiling_benchmark(scenario_id)
        return PlanBundle(
            scenario_id=scenario_id,
            official=official,
            profit_first=profit_first,
            current_price=current_price,
            theoretical_ceiling=theoretical_ceiling,
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
            {
                "upc": row["upc"],
                "candidate_rank": int(row["candidate_rank"]),
                "candidate_price": round(float(row["candidate_price"]), 2),
                "discount_pct": round(float(row["discount_pct"]), 4),
                "gross_profit": round(float(row["gross_profit"]), 4),
                "expected_units": round(float(row["expected_units"]), 4),
                "ending_inventory_units": round(float(row["ending_inventory_units"]), 4),
                "competitor_gap": round(float(row["competitor_gap"]), 4),
                **json.loads(row["evidence_json"]),
            }
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
        return SolveResult(
            run_id=run_id,
            scenario_id=run_row["scenario_id"],
            status=run_row["status"],
            summary=summary,
            phases=phases,
            diagnostics=diagnostics,
            selections=selections,
        )

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
        alternatives = []
        for row in sku_df.sort_values("discount_pct").itertuples(index=False):
            alternatives.append(
                {
                    "discount_pct": round(float(row.discount_pct), 4),
                    "candidate_price": round(float(row.candidate_price), 2),
                    "gross_profit": round(float(row.gross_profit), 4),
                    "expected_units": round(float(row.expected_units_capped), 4),
                    "effective_hard_valid": bool(row.effective_hard_valid),
                    "reason": row.effective_hard_violation_reason,
                    "is_selected": int(row.candidate_rank) == int(selected_row["candidate_rank"]),
                    "is_current": bool(row.is_current_price),
                }
            )

        return {
            "run_id": run_id,
            "scenario_id": run_row["scenario_id"],
            "upc": upc,
            "objective_mode": run_row["objective_mode"],
            "selected": self._row_to_evidence(selected_row),
            "current": self._row_to_evidence(current_row),
            "local_best_feasible": self._row_to_evidence(local_best_row),
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
        changed_rows = []
        for row in changed.sort_values("gross_profit_delta").itertuples(index=False):
            changed_rows.append(
                {
                    "upc": row.upc,
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
            "competitor_gap": round(float(row["effective_competitor_gap"]), 4),
            "effective_hard_valid": bool(row["effective_hard_valid"]),
            "reason": row["effective_hard_violation_reason"],
        }

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
