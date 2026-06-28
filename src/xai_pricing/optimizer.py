from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from itertools import product
import math
from time import perf_counter
import sqlite3
import uuid

import pandas as pd
import pulp

from .db import json_dumps, utc_now


OBJECTIVE_MODES: dict[str, tuple[tuple[str, str], ...]] = {
    "official": (
        ("gross_profit", "max"),
        ("competitor_gap", "min"),
        ("discount_depth", "min"),
    ),
    "position_first": (
        ("competitor_gap", "min"),
        ("gross_profit", "max"),
        ("discount_depth", "min"),
    ),
}
SOLVER_NAME = "pulp_highs"
INPUT_HASH_MODEL_VERSION = "2026-06-28-v2"
OFFICIAL_GROSS_PROFIT_TOLERANCE_PCT = 0.005


@dataclass(frozen=True)
class SolveRequest:
    scenario_id: str
    run_id: str | None = None
    run_kind: str = "official"
    source_run_id: str | None = None
    objective_mode: str = "official"
    budget_pct: float | None = None
    safety_stock_pct: float | None = None
    min_margin_overrides: dict[str, float] = field(default_factory=dict)
    competitor_tolerance_overrides: dict[str, float] = field(default_factory=dict)
    exact_discount_locks: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PhaseResult:
    phase_name: str
    status: str
    objective_value: float | None
    duration_ms: int
    details: dict[str, object]


@dataclass(frozen=True)
class SolveResult:
    run_id: str
    scenario_id: str
    status: str
    summary: dict[str, object]
    phases: list[PhaseResult]
    diagnostics: dict[str, object]
    selections: list[dict[str, object]]


class PricingOptimizer:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def solve(self, request: SolveRequest) -> SolveResult:
        scenario = self._load_scenario(request.scenario_id)
        run_id = request.run_id or self._default_run_id(request)
        phase_sequence = OBJECTIVE_MODES.get(request.objective_mode)
        if phase_sequence is None:
            raise RuntimeError(f"Unsupported objective_mode: {request.objective_mode}")
        effective_budget_pct = (
            float(request.budget_pct) if request.budget_pct is not None else float(scenario["budget_pct"])
        )
        effective_safety_stock_pct = float(scenario["safety_stock_pct"])
        candidate_df = self._load_candidate_frame(request.scenario_id)
        if candidate_df.empty:
            raise RuntimeError(f"No candidate_outcomes found for scenario {request.scenario_id}")
        self._validate_request(
            request=request,
            effective_budget_pct=effective_budget_pct,
            candidate_df=candidate_df,
        )
        input_hash = self._input_hash(
            request,
            budget_pct=effective_budget_pct,
            safety_stock_pct=effective_safety_stock_pct,
            candidate_df=candidate_df,
        )

        working_df, precheck = self._prepare_candidates(
            candidate_df,
            effective_budget_pct=effective_budget_pct,
            effective_safety_stock_pct=effective_safety_stock_pct,
            request=request,
        )
        diagnostics = {
            "scenario_id": request.scenario_id,
            "profile_id": scenario["profile_id"],
            "effective_budget_pct": effective_budget_pct,
            "effective_safety_stock_pct": effective_safety_stock_pct,
            "objective_mode": request.objective_mode,
            "gross_profit_tradeoff_tolerance_pct": self._gross_profit_tolerance_pct(request.objective_mode),
            "rule_inputs": {
                "min_margin_overrides": request.min_margin_overrides,
                "competitor_tolerance_overrides": request.competitor_tolerance_overrides,
                "exact_discount_locks": request.exact_discount_locks,
            },
            "precheck": precheck,
        }
        self._insert_run(
            run_id=run_id,
            request=request,
            input_hash=input_hash,
            budget_pct=effective_budget_pct,
            safety_stock_pct=effective_safety_stock_pct,
            diagnostics=diagnostics,
        )

        if precheck["status"] != "ready":
            self._complete_run(
                run_id=run_id,
                status="infeasible_precheck",
                diagnostics=diagnostics,
            )
            return SolveResult(
                run_id=run_id,
                scenario_id=request.scenario_id,
                status="infeasible_precheck",
                summary={"selected_products": 0},
                phases=[],
                diagnostics=diagnostics,
                selections=[],
            )

        phase_results: list[PhaseResult] = []
        phase_values: dict[str, float] = {}
        selected_df: pd.DataFrame | None = None
        status = "optimal"

        for phase_idx, (objective_key, sense) in enumerate(phase_sequence, start=1):
            phase_name = f"phase_{phase_idx}_{objective_key}"
            prior_bounds = phase_values.copy()
            phase_result, candidate_selection = self._solve_phase(
                phase_name=phase_name,
                objective_key=objective_key,
                sense=sense,
                working_df=working_df,
                effective_budget_pct=effective_budget_pct,
                phase_values=prior_bounds,
                exact_discount_locks=request.exact_discount_locks,
                gross_profit_tolerance_pct=self._gross_profit_tolerance_pct(request.objective_mode),
            )
            phase_results.append(phase_result)
            self._insert_phase(run_id, phase_result)
            if phase_result.status != "Optimal":
                status = "solver_failed"
                diagnostics["failed_phase"] = phase_name
                diagnostics["failed_status"] = phase_result.status
                break
            phase_values[objective_key] = float(phase_result.objective_value or 0.0)
            selected_df = candidate_selection

        selections: list[dict[str, object]] = []
        summary: dict[str, object] = {"selected_products": 0}
        if status == "optimal" and selected_df is not None:
            selected_df = self._attach_local_ranks(working_df, selected_df)
            selections = self._serialize_selections(selected_df)
            summary = self._build_summary(selected_df, effective_budget_pct)
            diagnostics["discount_distribution"] = summary["discount_distribution"]
            diagnostics["selected_archetypes"] = summary["selected_archetypes"]
            self._insert_items(run_id, selected_df)
            self._complete_run(
                run_id=run_id,
                status=status,
                diagnostics={**diagnostics, "summary": summary},
            )
        else:
            self._complete_run(run_id=run_id, status=status, diagnostics=diagnostics)

        return SolveResult(
            run_id=run_id,
            scenario_id=request.scenario_id,
            status=status,
            summary=summary,
            phases=phase_results,
            diagnostics=diagnostics,
            selections=selections,
        )

    def _load_scenario(self, scenario_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            """
            SELECT scenario_id, scenario_name, profile_id, budget_pct, safety_stock_pct
            FROM scenarios
            WHERE scenario_id = ?
            """,
            (scenario_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Scenario not found: {scenario_id}")
        return row

    def _load_candidate_frame(self, scenario_id: str) -> pd.DataFrame:
        return pd.read_sql(
            """
            SELECT
                c.scenario_id,
                c.upc,
                c.candidate_rank,
                c.candidate_price,
                c.discount_pct,
                c.expected_units,
                c.conservative_units,
                c.optimistic_units,
                c.revenue,
                c.gross_profit,
                c.gross_margin_pct,
                c.competitor_index,
                c.inventory_cap_units,
                c.expected_units_capped,
                c.is_current_price,
                c.is_reference_price,
                c.list_price,
                c.markdown_investment,
                c.ending_inventory_units,
                c.expected_lost_units,
                c.optimistic_lost_units,
                c.competitor_gap,
                c.is_hard_valid,
                c.hard_violation_reason,
                p.strategic_role,
                p.archetype,
                p.min_margin_pct,
                p.max_discount_pct,
                p.competitor_tolerance_pct,
                p.competitor_weight,
                p.reference_price,
                p.unit_cost,
                d.baseline_units,
                i.on_hand_units,
                i.inbound_units,
                i.safety_stock_units
            FROM candidate_outcomes c
            JOIN product_context p
                ON p.scenario_id = c.scenario_id
               AND p.upc = c.upc
            JOIN demand_models d
                ON d.scenario_id = c.scenario_id
               AND d.upc = c.upc
            JOIN inventory_positions i
                ON i.scenario_id = c.scenario_id
               AND i.upc = c.upc
            WHERE c.scenario_id = ?
            ORDER BY c.upc, c.candidate_rank
            """,
            self.conn,
            params=(scenario_id,),
        )

    def _prepare_candidates(
        self,
        candidate_df: pd.DataFrame,
        *,
        effective_budget_pct: float,
        effective_safety_stock_pct: float,
        request: SolveRequest,
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        df = candidate_df.copy()
        df["effective_min_margin_pct"] = df["upc"].map(request.min_margin_overrides).fillna(df["min_margin_pct"])
        df["effective_competitor_tolerance_pct"] = (
            df["upc"].map(request.competitor_tolerance_overrides).fillna(df["competitor_tolerance_pct"])
        )
        df["effective_safety_stock_units"] = 0.0
        df["inventory_buffer_units"] = df["ending_inventory_units"]
        df["margin_valid"] = df["gross_margin_pct"] + 1e-9 >= df["effective_min_margin_pct"]
        df["inventory_valid"] = df["inventory_buffer_units"] + 1e-9 >= 0
        df["discount_valid"] = df["discount_pct"] <= df["max_discount_pct"] + 1e-9
        df["effective_hard_violation_reason"] = df.apply(self._derive_effective_violation_reason, axis=1)
        df["effective_hard_valid"] = df["margin_valid"] & df["inventory_valid"] & df["discount_valid"]
        df["effective_competitor_gap"] = (
            df["competitor_index"] - (1 + df["effective_competitor_tolerance_pct"])
        ).clip(lower=0.0)
        df["budget_coeff"] = df["markdown_investment"] - (
            effective_budget_pct * df["list_price"] * df["expected_units_capped"]
        )

        by_upc = df.groupby("upc")["effective_hard_valid"].sum()
        invalid_upcs = by_upc[by_upc == 0].index.tolist()
        precheck = {
            "status": "ready",
            "sku_count": int(df["upc"].nunique()),
            "candidate_count": int(len(df)),
            "eligible_candidate_count": int(df["effective_hard_valid"].sum()),
            "invalid_skus": invalid_upcs,
            "lock_conflicts": [],
            "global_conflicts": [],
            "promotable_skus": int(
                df[(df["effective_hard_valid"]) & (df["discount_pct"] > 0)]["upc"].nunique()
            ),
            "hard_violation_counts": {
                "margin": int((~df["margin_valid"]).sum()),
                "inventory": int((~df["inventory_valid"]).sum()),
                "discount": int((~df["discount_valid"]).sum()),
            },
            "hard_violation_examples": self._summarize_violation_examples(df),
        }
        if invalid_upcs:
            precheck["status"] = "infeasible"

        if request.exact_discount_locks:
            for upc, discount_pct in request.exact_discount_locks.items():
                locked = df[
                    (df["upc"] == upc)
                    & (df["discount_pct"].sub(discount_pct).abs() < 1e-9)
                ]
                if locked.empty:
                    precheck["lock_conflicts"].append(
                        {"upc": upc, "reason": "candidate_missing", "discount_pct": discount_pct}
                    )
                    continue
                locked_row = locked.iloc[0]
                if not bool(locked_row["effective_hard_valid"]):
                    precheck["lock_conflicts"].append(
                        {
                            "upc": upc,
                            "reason": "candidate_invalid",
                            "discount_pct": discount_pct,
                            "hard_violation_reason": locked_row["effective_hard_violation_reason"],
                        }
                    )
            if precheck["lock_conflicts"]:
                precheck["status"] = "infeasible"

        budget_floor = self._budget_floor_analysis(
            df,
            exact_discount_locks=request.exact_discount_locks,
            effective_budget_pct=effective_budget_pct,
        )
        precheck["budget_floor"] = budget_floor
        if not budget_floor["feasible"]:
            precheck["status"] = "infeasible"
            precheck["global_conflicts"].append(
                {
                    "reason": "portfolio_budget",
                    "minimum_budget_utilization_pct": budget_floor["minimum_budget_utilization_pct"],
                    "budget_limit_pct": effective_budget_pct,
                    "examples": budget_floor["tight_examples"],
                }
            )

        return df, precheck

    def _solve_phase(
        self,
        *,
        phase_name: str,
        objective_key: str,
        sense: str,
        working_df: pd.DataFrame,
        effective_budget_pct: float,
        phase_values: dict[str, float],
        exact_discount_locks: dict[str, float],
        gross_profit_tolerance_pct: float,
    ) -> tuple[PhaseResult, pd.DataFrame]:
        eligible_df = working_df[working_df["effective_hard_valid"]].copy()
        model = pulp.LpProblem(
            phase_name,
            pulp.LpMinimize if sense == "min" else pulp.LpMaximize,
        )
        variable_map: dict[tuple[str, int], pulp.LpVariable] = {}
        for row in eligible_df.itertuples(index=False):
            variable_map[(row.upc, row.candidate_rank)] = pulp.LpVariable(
                f"x_{row.upc}_{row.candidate_rank}",
                lowBound=0,
                upBound=1,
                cat="Binary",
            )

        for upc, group in eligible_df.groupby("upc"):
            model += (
                pulp.lpSum(variable_map[(upc, candidate_rank)] for candidate_rank in group["candidate_rank"]) == 1,
                f"select_one_{upc}",
            )
            if upc in exact_discount_locks:
                locked_discount_pct = exact_discount_locks[upc]
                locked_candidates = [
                    variable_map[(upc, candidate_rank)]
                    for candidate_rank, discount_pct in zip(
                        group["candidate_rank"],
                        group["discount_pct"],
                        strict=True,
                    )
                    if abs(float(discount_pct) - float(locked_discount_pct)) < 1e-9
                ]
                model += (pulp.lpSum(locked_candidates) == 1, f"lock_discount_{upc}")

        model += (
            pulp.lpSum(
                float(row.budget_coeff) * variable_map[(row.upc, row.candidate_rank)]
                for row in eligible_df.itertuples(index=False)
            )
            <= 0,
            "budget_limit",
        )

        for phase_key, value in phase_values.items():
            if phase_key == "competitor_gap":
                coeffs = [
                    float(row.effective_competitor_gap) * int(row.competitor_weight)
                    for row in eligible_df.itertuples(index=False)
                ]
                lhs = pulp.lpSum(
                    coeff * variable_map[(row.upc, row.candidate_rank)]
                    for coeff, row in zip(coeffs, eligible_df.itertuples(index=False), strict=True)
                )
                model += (lhs <= value + 1e-6, "lock_competitor_gap")
            elif phase_key == "gross_profit":
                coeffs = [float(row.gross_profit) for row in eligible_df.itertuples(index=False)]
                lhs = pulp.lpSum(
                    coeff * variable_map[(row.upc, row.candidate_rank)]
                    for coeff, row in zip(coeffs, eligible_df.itertuples(index=False), strict=True)
                )
                tolerated_value = value * (1 - gross_profit_tolerance_pct)
                model += (lhs >= tolerated_value - 1e-6, "lock_gross_profit")

        objective = self._build_objective(objective_key, eligible_df, variable_map)
        model += objective

        solver = pulp.HiGHS(msg=False)
        start = perf_counter()
        solver_status = model.solve(solver)
        duration_ms = int((perf_counter() - start) * 1000)
        status_text = pulp.LpStatus[solver_status]
        objective_value = None if status_text != "Optimal" else float(pulp.value(model.objective))
        details = {
            "variable_count": len(variable_map),
            "constraint_count": len(model.constraints),
            "budget_pct": effective_budget_pct,
            "lock_count": len(exact_discount_locks),
            "gross_profit_tolerance_pct": gross_profit_tolerance_pct,
        }
        phase_result = PhaseResult(
            phase_name=phase_name,
            status=status_text,
            objective_value=objective_value,
            duration_ms=duration_ms,
            details=details,
        )
        if status_text != "Optimal":
            return phase_result, pd.DataFrame()

        chosen_keys = [key for key, variable in variable_map.items() if variable.value() and variable.value() > 0.5]
        selected_df = eligible_df.set_index(["upc", "candidate_rank"]).loc[chosen_keys].reset_index()
        return phase_result, selected_df

    def _build_objective(
        self,
        objective_key: str,
        eligible_df: pd.DataFrame,
        variable_map: dict[tuple[str, int], pulp.LpVariable],
    ) -> pulp.LpAffineExpression:
        terms = []
        for row in eligible_df.itertuples(index=False):
            variable = variable_map[(row.upc, row.candidate_rank)]
            if objective_key == "competitor_gap":
                coeff = float(row.effective_competitor_gap) * int(row.competitor_weight)
            elif objective_key == "gross_profit":
                coeff = float(row.gross_profit)
            else:
                coeff = float(row.discount_pct)
            terms.append(coeff * variable)
        return pulp.lpSum(terms)

    def _attach_local_ranks(self, working_df: pd.DataFrame, selected_df: pd.DataFrame) -> pd.DataFrame:
        feasible = working_df[working_df["effective_hard_valid"]].copy()
        feasible["local_gp_rank"] = (
            feasible.groupby("upc")["gross_profit"].rank(method="dense", ascending=False).astype(int)
        )
        feasible["local_gap_rank"] = (
            feasible.groupby("upc")["effective_competitor_gap"].rank(method="dense", ascending=True).astype(int)
        )
        merged = selected_df.merge(
            feasible[
                [
                    "upc",
                    "candidate_rank",
                    "local_gp_rank",
                    "local_gap_rank",
                ]
            ],
            on=["upc", "candidate_rank"],
            how="left",
        )
        merged["selection_rank"] = merged["discount_pct"].rank(method="dense", ascending=False).astype(int)
        return merged

    def _serialize_selections(self, selected_df: pd.DataFrame) -> list[dict[str, object]]:
        records = []
        for row in selected_df.sort_values(["discount_pct", "gross_profit"], ascending=[False, False]).itertuples(index=False):
            records.append(
                {
                    "upc": row.upc,
                    "candidate_rank": int(row.candidate_rank),
                    "candidate_price": round(float(row.candidate_price), 2),
                    "discount_pct": round(float(row.discount_pct), 4),
                    "gross_profit": round(float(row.gross_profit), 4),
                    "expected_units": round(float(row.expected_units_capped), 4),
                    "ending_inventory_units": round(float(row.ending_inventory_units), 4),
                    "optimistic_lost_units": round(float(row.optimistic_lost_units), 4),
                    "competitor_gap": round(float(row.effective_competitor_gap), 4),
                    "archetype": row.archetype,
                    "role": row.strategic_role,
                    "local_gp_rank": int(row.local_gp_rank),
                    "local_gap_rank": int(row.local_gap_rank),
                }
            )
        return records

    def _build_summary(self, selected_df: pd.DataFrame, effective_budget_pct: float) -> dict[str, object]:
        total_list_revenue = float((selected_df["list_price"] * selected_df["expected_units_capped"]).sum())
        total_markdown = float(selected_df["markdown_investment"].sum())
        total_gp = float(selected_df["gross_profit"].sum())
        total_revenue = float(selected_df["revenue"].sum())
        weighted_gap = float((selected_df["effective_competitor_gap"] * selected_df["competitor_weight"]).sum())
        budget_utilization = 0.0 if total_list_revenue <= 0 else total_markdown / total_list_revenue
        discount_distribution = {
            f"{int(round(discount * 100))}%": int(count)
            for discount, count in (
                selected_df["discount_pct"].round(4).value_counts().sort_index().items()
            )
        }
        archetypes = {
            key: int(value)
            for key, value in selected_df["archetype"].value_counts().sort_index().items()
        }
        return {
            "selected_products": int(len(selected_df)),
            "promoted_products": int((selected_df["discount_pct"] > 0).sum()),
            "protected_products": int((selected_df["discount_pct"] == 0).sum()),
            "total_revenue": round(total_revenue, 2),
            "total_gross_profit": round(total_gp, 2),
            "total_markdown_investment": round(total_markdown, 2),
            "budget_utilization_pct": round(budget_utilization, 4),
            "budget_limit_pct": effective_budget_pct,
            "weighted_competitor_gap": round(weighted_gap, 4),
            "discount_distribution": discount_distribution,
            "selected_archetypes": archetypes,
            "inventory_tight_products": int((selected_df["ending_inventory_units"] <= selected_df["baseline_units"] * 0.15).sum()),
            "upside_risk_products": int((selected_df["optimistic_lost_units"] > 0).sum()),
        }

    def _insert_run(
        self,
        *,
        run_id: str,
        request: SolveRequest,
        input_hash: str,
        budget_pct: float,
        safety_stock_pct: float,
        diagnostics: dict[str, object],
    ) -> None:
        existing_run = self.conn.execute(
            "SELECT run_id, run_kind, status FROM optimizer_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if existing_run is not None:
            raise RuntimeError(
                f"Run ID already exists and cannot be overwritten: {run_id} "
                f"({existing_run['run_kind']}, {existing_run['status']})"
            )
        self.conn.execute(
            """
            INSERT INTO optimizer_runs (
                run_id, scenario_id, source_run_id, run_kind, status, solver_name,
                objective_mode, input_hash, budget_pct, safety_stock_pct,
                diagnostics_json, created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                request.scenario_id,
                request.source_run_id,
                request.run_kind,
                "running",
                SOLVER_NAME,
                request.objective_mode,
                input_hash,
                budget_pct,
                safety_stock_pct,
                json_dumps(diagnostics),
                utc_now(),
                None,
            ),
        )
        self.conn.commit()

    def _insert_phase(self, run_id: str, phase_result: PhaseResult) -> None:
        self.conn.execute(
            """
            INSERT INTO optimizer_run_phases (
                run_id, phase_name, status, objective_value, duration_ms, details_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                phase_result.phase_name,
                phase_result.status,
                phase_result.objective_value,
                phase_result.duration_ms,
                json_dumps(phase_result.details),
            ),
        )
        self.conn.commit()

    def _insert_items(self, run_id: str, selected_df: pd.DataFrame) -> None:
        for row in selected_df.itertuples(index=False):
            evidence = {
                "archetype": row.archetype,
                "strategic_role": row.strategic_role,
                "local_gp_rank": int(row.local_gp_rank),
                "local_gap_rank": int(row.local_gap_rank),
                "inventory_buffer_units": round(float(row.inventory_buffer_units), 4),
                "effective_safety_stock_units": round(float(row.effective_safety_stock_units), 4),
                "effective_competitor_gap": round(float(row.effective_competitor_gap), 4),
                "effective_min_margin_pct": round(float(row.effective_min_margin_pct), 4),
                "optimistic_lost_units": round(float(row.optimistic_lost_units), 4),
            }
            self.conn.execute(
                """
                INSERT INTO optimizer_run_items (
                    run_id, upc, candidate_rank, candidate_price, discount_pct,
                    expected_units, revenue, gross_profit, markdown_investment,
                    ending_inventory_units, competitor_index, competitor_gap,
                    selection_rank, evidence_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row.upc,
                    int(row.candidate_rank),
                    round(float(row.candidate_price), 2),
                    round(float(row.discount_pct), 4),
                    round(float(row.expected_units_capped), 4),
                    round(float(row.revenue), 4),
                    round(float(row.gross_profit), 4),
                    round(float(row.markdown_investment), 4),
                    round(float(row.ending_inventory_units), 4),
                    row.competitor_index,
                    round(float(row.effective_competitor_gap), 4),
                    int(row.selection_rank),
                    json_dumps(evidence),
                ),
            )
        self.conn.commit()

    def _complete_run(self, *, run_id: str, status: str, diagnostics: dict[str, object]) -> None:
        self.conn.execute(
            """
            UPDATE optimizer_runs
            SET status = ?, diagnostics_json = ?, completed_at = ?
            WHERE run_id = ?
            """,
            (status, json_dumps(diagnostics), utc_now(), run_id),
        )
        self.conn.commit()

    def _default_run_id(self, request: SolveRequest) -> str:
        return f"{request.scenario_id}_{request.run_kind}_{request.objective_mode}_{uuid.uuid4().hex[:8]}"

    def _derive_effective_violation_reason(self, row: pd.Series) -> str | None:
        reasons: list[str] = []
        if not bool(row["margin_valid"]):
            reasons.append("margin")
        if not bool(row["inventory_valid"]):
            reasons.append("inventory")
        if not bool(row["discount_valid"]):
            reasons.append("discount_limit")
        return ",".join(reasons) if reasons else None

    def _summarize_violation_examples(self, df: pd.DataFrame) -> list[dict[str, object]]:
        invalid_df = df[~df["effective_hard_valid"]].copy()
        if invalid_df.empty:
            return []
        columns = [
            "upc",
            "discount_pct",
            "effective_hard_violation_reason",
            "gross_margin_pct",
            "effective_min_margin_pct",
            "ending_inventory_units",
            "effective_safety_stock_units",
            "max_discount_pct",
        ]
        samples = invalid_df.sort_values(["upc", "discount_pct"], ascending=[True, False])[columns].head(8)
        records: list[dict[str, object]] = []
        for row in samples.itertuples(index=False):
            records.append(
                {
                    "upc": row.upc,
                    "discount_pct": round(float(row.discount_pct), 4),
                    "reason": row.effective_hard_violation_reason,
                    "gross_margin_pct": round(float(row.gross_margin_pct), 4),
                    "min_margin_pct": round(float(row.effective_min_margin_pct), 4),
                    "ending_inventory_units": round(float(row.ending_inventory_units), 4),
                    "safety_stock_units": round(float(row.effective_safety_stock_units), 4),
                    "max_discount_pct": round(float(row.max_discount_pct), 4),
                }
            )
        return records

    def preview_input_hash(self, request: SolveRequest) -> tuple[str, float, float]:
        scenario = self._load_scenario(request.scenario_id)
        effective_budget_pct = (
            float(request.budget_pct) if request.budget_pct is not None else float(scenario["budget_pct"])
        )
        effective_safety_stock_pct = float(scenario["safety_stock_pct"])
        candidate_df = self._load_candidate_frame(request.scenario_id)
        if candidate_df.empty:
            raise RuntimeError(f"No candidate_outcomes found for scenario {request.scenario_id}")
        self._validate_request(
            request=request,
            effective_budget_pct=effective_budget_pct,
            candidate_df=candidate_df,
        )
        return (
            self._input_hash(
                request,
                budget_pct=effective_budget_pct,
                safety_stock_pct=effective_safety_stock_pct,
                candidate_df=candidate_df,
            ),
            effective_budget_pct,
            effective_safety_stock_pct,
        )

    def _validate_request(
        self,
        *,
        request: SolveRequest,
        effective_budget_pct: float,
        candidate_df: pd.DataFrame,
    ) -> None:
        if not 0.0 <= effective_budget_pct <= 1.0:
            raise ValueError("budget_pct must be between 0.0 and 1.0")

        valid_upcs = set(candidate_df["upc"].astype(str).unique().tolist())
        for upc, margin in request.min_margin_overrides.items():
            if upc not in valid_upcs:
                raise ValueError(f"Unknown SKU in min_margin_overrides: {upc}")
            if not 0.0 <= float(margin) <= 1.0:
                raise ValueError(f"min_margin_pct override must be between 0.0 and 1.0 for {upc}")

        for upc, tolerance in request.competitor_tolerance_overrides.items():
            if upc not in valid_upcs:
                raise ValueError(f"Unknown SKU in competitor_tolerance_overrides: {upc}")
            if not 0.0 <= float(tolerance) <= 1.0:
                raise ValueError(f"competitor_tolerance_pct override must be between 0.0 and 1.0 for {upc}")

        for upc, discount in request.exact_discount_locks.items():
            if upc not in valid_upcs:
                raise ValueError(f"Unknown SKU in exact_discount_locks: {upc}")
            if not 0.0 <= float(discount) <= 1.0:
                raise ValueError(f"Discount lock for {upc} must be between 0.0 and 1.0")

    def _budget_floor_analysis(
        self,
        df: pd.DataFrame,
        *,
        exact_discount_locks: dict[str, float],
        effective_budget_pct: float,
    ) -> dict[str, object]:
        feasible_df = df[df["effective_hard_valid"]].copy()
        selected_rows: list[pd.Series] = []
        for upc, group in feasible_df.groupby("upc"):
            options = group
            if upc in exact_discount_locks:
                options = group[group["discount_pct"].sub(float(exact_discount_locks[upc])).abs() < 1e-9]
            if options.empty:
                return {
                    "feasible": False,
                    "minimum_budget_coeff": None,
                    "minimum_budget_utilization_pct": None,
                    "tight_examples": [],
                }
            selected_rows.append(options.sort_values(["budget_coeff", "discount_pct"]).iloc[0])

        floor_df = pd.DataFrame(selected_rows)
        minimum_budget_coeff = float(floor_df["budget_coeff"].sum()) if not floor_df.empty else 0.0
        total_markdown = float(floor_df["markdown_investment"].sum()) if not floor_df.empty else 0.0
        total_list_revenue = float((floor_df["list_price"] * floor_df["expected_units_capped"]).sum()) if not floor_df.empty else 0.0
        minimum_budget_utilization_pct = 0.0 if total_list_revenue <= 0 else total_markdown / total_list_revenue
        examples = (
            floor_df.sort_values(["budget_coeff", "discount_pct"], ascending=[False, False])
            .head(5)[["upc", "discount_pct", "budget_coeff", "markdown_investment"]]
            .to_dict("records")
        )
        return {
            "feasible": minimum_budget_coeff <= 1e-9,
            "minimum_budget_coeff": round(minimum_budget_coeff, 4),
            "minimum_budget_utilization_pct": round(minimum_budget_utilization_pct, 4),
            "tight_examples": [
                {
                    "upc": str(row["upc"]),
                    "discount_pct": round(float(row["discount_pct"]), 4),
                    "budget_coeff": round(float(row["budget_coeff"]), 4),
                    "markdown_investment": round(float(row["markdown_investment"]), 4),
                }
                for row in examples
            ],
        }

    def _candidate_frame_digest(self, candidate_df: pd.DataFrame) -> str:
        ordered = candidate_df.sort_values(["upc", "candidate_rank"]).reset_index(drop=True)
        payload = ordered.to_dict("records")
        return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()

    def _gross_profit_tolerance_pct(self, objective_mode: str) -> float:
        if objective_mode == "official":
            return OFFICIAL_GROSS_PROFIT_TOLERANCE_PCT
        return 0.0

    def _input_hash(
        self,
        request: SolveRequest,
        budget_pct: float,
        safety_stock_pct: float,
        candidate_df: pd.DataFrame,
    ) -> str:
        payload = asdict(request)
        payload["input_hash_model_version"] = INPUT_HASH_MODEL_VERSION
        payload["budget_pct"] = budget_pct
        payload["safety_stock_pct"] = safety_stock_pct
        payload["candidate_frame_digest"] = self._candidate_frame_digest(candidate_df)
        return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def format_solve_report(result: SolveResult) -> str:
    lines = [
        f"Run ID:        {result.run_id}",
        f"Scenario:      {result.scenario_id}",
        f"Run status:    {result.status}",
        "",
        "Precheck",
        f"  Status:      {result.diagnostics['precheck']['status']}",
        f"  SKUs:        {result.diagnostics['precheck']['sku_count']}",
        f"  Candidates:  {result.diagnostics['precheck']['candidate_count']}",
        f"  Eligible:    {result.diagnostics['precheck']['eligible_candidate_count']}",
        f"  Promo-ready: {result.diagnostics['precheck']['promotable_skus']}",
        "",
        "OR phases",
    ]
    for phase in result.phases:
        objective = "n/a" if phase.objective_value is None else f"{phase.objective_value:.4f}"
        lines.append(
            f"  {phase.phase_name}: status={phase.status} objective={objective} duration_ms={phase.duration_ms}"
        )
    if result.status == "optimal":
        summary = result.summary
        lines.extend(
            [
                "",
                "Result",
                f"  Selected SKUs:      {summary['selected_products']}",
                f"  Promoted SKUs:      {summary['promoted_products']}",
                f"  Protected SKUs:     {summary['protected_products']}",
                f"  Revenue:            {summary['total_revenue']:.2f}",
                f"  Gross profit:       {summary['total_gross_profit']:.2f}",
                f"  Markdown spend:     {summary['total_markdown_investment']:.2f}",
                f"  Budget utilization: {summary['budget_utilization_pct']:.2%} of list revenue",
                f"  Weighted comp gap:  {summary['weighted_competitor_gap']:.4f}",
                f"  Discount mix:       {summary['discount_distribution']}",
                f"  Archetypes:         {summary['selected_archetypes']}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Diagnostics",
                f"  Invalid SKUs:  {result.diagnostics['precheck']['invalid_skus']}",
                f"  Lock issues:   {result.diagnostics['precheck']['lock_conflicts']}",
            ]
        )
    return "\n".join(lines)
