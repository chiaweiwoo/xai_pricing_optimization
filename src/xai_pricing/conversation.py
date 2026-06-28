from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any
from urllib import error, request

from . import config as _config  # Ensures .env is loaded before reading environment variables.
from .db import json_dumps
from .planner import CounterfactualResult, PlanBundle, PricingDecisionService


SUPPORTED_INTENTS = {
    "PLAN_SUMMARY",
    "WHY_SELECTED",
    "WHY_NOT",
    "OVERRIDE_WHAT_IF",
    "RULE_WHAT_IF",
    "HELP",
    "CLARIFY",
    "UNSUPPORTED",
}


INTENT_SCOPE_TEXT = {
    "PLAN_SUMMARY": "Summarize the official proposal and compare it with key benchmarks.",
    "WHY_SELECTED": "Explain why one product received its selected discount.",
    "WHY_NOT": "Explain why a different discrete discount was not selected for one product.",
    "OVERRIDE_WHAT_IF": "Force one product to a discrete discount and re-solve a separate child scenario.",
    "RULE_WHAT_IF": "Change one safe rule such as budget, minimum margin, or competitor tolerance.",
    "HELP": "Show supported question types and example prompts.",
    "CLARIFY": "Ask for one missing field needed to answer a supported question.",
    "UNSUPPORTED": "Question is outside the supported assistant scope.",
}


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    intent: dict[str, Any]
    response_text: str
    evidence: dict[str, Any]
    presentation: dict[str, Any]


class DeepSeekClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
        self.thinking = os.getenv("DEEPSEEK_THINKING", "disabled").strip() or "disabled"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def chat_json(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        raw = self._request_chat(system_prompt=system_prompt, user_prompt=user_prompt, json_output=True)
        return json.loads(raw)

    def _request_chat(self, *, system_prompt: str, user_prompt: str, json_output: bool) -> str:
        if not self.configured:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": self.thinking},
            "stream": False,
        }
        if json_output:
            payload["response_format"] = {"type": "json_object"}

        req = request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json_dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"DeepSeek API error: HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"DeepSeek API connection error: {exc.reason}") from exc

        return body["choices"][0]["message"]["content"]


class PricingConversationService:
    def __init__(
        self,
        planner: PricingDecisionService,
        *,
        llm_client: DeepSeekClient | None = None,
    ) -> None:
        self.planner = planner
        self.llm_client = llm_client or DeepSeekClient()

    def handle_question(self, plan: PlanBundle, question: str) -> ConversationTurn:
        intent = self._classify_intent(question=question, plan=plan)
        evidence = self._build_evidence(plan=plan, intent=intent, question=question)
        presentation = self._build_presentation(question=question, intent=intent, evidence=evidence, plan=plan)
        response_text = self._render_presentation(presentation)
        return ConversationTurn(
            question=question,
            intent=intent,
            response_text=response_text,
            evidence=evidence,
            presentation=presentation,
        )

    def _classify_intent(self, *, question: str, plan: PlanBundle) -> dict[str, Any]:
        fallback = self._fallback_intent(question, plan)
        if not self.llm_client.configured:
            return fallback

        product_reference_rows = [
            {
                "upc": row["upc"],
                "product_label": row["product_label"],
                "category": row["category"],
            }
            for row in plan.catalog[:12]
        ]
        prompt = f"""
You classify questions for a bounded pricing-planning assistant.

Return JSON only.

Choose exactly one intent from:
- PLAN_SUMMARY
- WHY_SELECTED
- WHY_NOT
- OVERRIDE_WHAT_IF
- RULE_WHAT_IF
- HELP
- CLARIFY
- UNSUPPORTED

Supported override scope:
- exact product discount lock
- discount must be one of the allowed discrete buckets

Supported rule what-if scope:
- budget_pct
- min_margin_pct
- competitor_tolerance_pct

Scenario context:
- scenario_id: {plan.scenario_id}
- official_run_id: {plan.official.run_id}
- allowed_discount_buckets: {self.planner.get_allowed_discount_buckets(plan.scenario_id)}
- example products: {json.dumps(product_reference_rows)}

Output schema:
{{
  "intent": "...",
  "upc": "..." | null,
  "product_query": "..." | null,
  "discount_pct": 0.10 | null,
  "rule_name": "..." | null,
  "rule_value": 0.08 | null,
  "confidence": 0.0,
  "missing_fields": ["upc" | "discount_pct" | "rule_value"],
  "rationale": "short reason"
}}

Rules:
- If a product is mentioned by name rather than UPC, fill product_query.
- Use CLARIFY when the question is in scope but missing a required product, discount, or rule value.
- Use UNSUPPORTED when the request asks for actions outside the listed scope.
""".strip()
        try:
            candidate = self.llm_client.chat_json(system_prompt=prompt, user_prompt=question)
        except Exception:
            return fallback
        return self._validate_intent(candidate, fallback=fallback, plan=plan)

    def _validate_intent(self, candidate: dict[str, Any], *, fallback: dict[str, Any], plan: PlanBundle) -> dict[str, Any]:
        intent_name = str(candidate.get("intent", fallback["intent"])).upper()
        if intent_name not in SUPPORTED_INTENTS:
            return fallback

        upc = candidate.get("upc")
        product_query = candidate.get("product_query")
        resolved_upc = self._resolve_upc_reference(plan, upc or product_query)
        if resolved_upc is not None:
            upc = resolved_upc
        elif upc is not None or product_query:
            upc = None

        discount_pct = self._coerce_discount(candidate.get("discount_pct"))
        rule_name = candidate.get("rule_name")
        rule_value = candidate.get("rule_value")
        if rule_value is not None:
            try:
                rule_value = float(rule_value)
            except (TypeError, ValueError):
                rule_value = None
        missing_fields = [str(item) for item in candidate.get("missing_fields", []) if str(item)]
        validated = {
            "intent": intent_name,
            "upc": upc,
            "discount_pct": discount_pct,
            "rule_name": rule_name,
            "rule_value": rule_value,
            "confidence": float(candidate.get("confidence", fallback.get("confidence", 0.0))),
            "rationale": candidate.get("rationale", ""),
            "missing_fields": missing_fields,
            "scope": INTENT_SCOPE_TEXT.get(intent_name, INTENT_SCOPE_TEXT["UNSUPPORTED"]),
        }
        if intent_name in {"WHY_SELECTED", "WHY_NOT", "OVERRIDE_WHAT_IF"} and upc is None:
            validated["intent"] = "CLARIFY"
            validated["missing_fields"] = ["upc"]
        if intent_name in {"WHY_NOT", "OVERRIDE_WHAT_IF"} and discount_pct is None:
            validated["intent"] = "CLARIFY"
            validated["missing_fields"] = ["discount_pct"]
        if intent_name == "RULE_WHAT_IF" and rule_name is None:
            validated["intent"] = "CLARIFY"
            validated["missing_fields"] = ["rule_value"]
        return validated

    def _build_evidence(self, *, plan: PlanBundle, intent: dict[str, Any], question: str) -> dict[str, Any]:
        name = intent["intent"]
        if name == "PLAN_SUMMARY":
            return self._plan_summary_evidence(plan)
        if name == "WHY_SELECTED":
            dossier = self.planner.get_sku_dossier(plan.official.run_id, intent["upc"])
            current_lock = self._simulate_locked_discount(plan, dossier["upc"], float(dossier["current"]["discount_pct"]))
            local_best_lock = self._simulate_locked_discount(
                plan,
                dossier["upc"],
                float(dossier["local_best_feasible"]["discount_pct"]),
            )
            return {
                "intent": name,
                "question": question,
                "sku_dossier": dossier,
                "current_counterfactual": current_lock,
                "local_best_counterfactual": local_best_lock,
                "selection_analysis": self._build_selection_analysis(
                    plan=plan,
                    dossier=dossier,
                    current_counterfactual=current_lock,
                    local_best_counterfactual=local_best_lock,
                ),
                "benchmark_comparison": self._plan_summary_evidence(plan)["benchmark_comparison"],
            }
        if name == "WHY_NOT":
            dossier = self.planner.get_sku_dossier(plan.official.run_id, intent["upc"])
            target = None
            for alt in dossier["alternatives"]:
                if abs(float(alt["discount_pct"]) - float(intent["discount_pct"] or -1)) < 1e-9:
                    target = alt
                    break
            alternative_counterfactual = None
            if target is not None and target["effective_hard_valid"]:
                alternative_counterfactual = self._simulate_locked_discount(
                    plan,
                    dossier["upc"],
                    float(target["discount_pct"]),
                )
            return {
                "intent": name,
                "question": question,
                "sku_dossier": dossier,
                "target_alternative": target,
                "alternative_counterfactual": alternative_counterfactual,
            }
        if name == "OVERRIDE_WHAT_IF":
            counterfactual = self.planner.simulate_counterfactual(
                plan.official.run_id,
                exact_discount_locks={intent["upc"]: float(intent["discount_pct"] or 0.0)},
            )
            return self._counterfactual_evidence(counterfactual, plan=plan)
        if name == "RULE_WHAT_IF":
            return self._rule_what_if_evidence(plan=plan, intent=intent)
        if name == "HELP":
            return {
                "intent": "HELP",
                "starter_questions": self._starter_questions(plan),
            }
        if name == "CLARIFY":
            return {
                "intent": "CLARIFY",
                "message": "The question is in scope, but I need one more field before I can answer it.",
                "missing_fields": intent.get("missing_fields", []),
                "starter_questions": self._starter_questions(plan),
            }
        return {
            "intent": "UNSUPPORTED",
            "message": "Supported questions are limited to plan summary, why or why not for one product, and a few safe what-if rules.",
            "starter_questions": self._starter_questions(plan),
        }

    def _plan_summary_evidence(self, plan: PlanBundle) -> dict[str, Any]:
        official = plan.official.summary
        position_first = plan.position_first.summary
        ceiling = plan.theoretical_ceiling.summary
        current = plan.current_price.summary
        return {
            "intent": "PLAN_SUMMARY",
            "plan_brief": plan.brief,
            "official": official,
            "official_run_id": plan.official.run_id,
            "position_first": position_first,
            "current_price": current,
            "theoretical_ceiling": ceiling,
            "benchmark_comparison": {
                "vs_current_gp": round(float(official["total_gross_profit"]) - float(current["total_gross_profit"]), 2),
                "vs_current_revenue": round(float(official["total_revenue"]) - float(current["total_revenue"]), 2),
                "vs_position_first_gp": round(
                    float(official["total_gross_profit"]) - float(position_first["total_gross_profit"]),
                    2,
                ),
                "vs_ceiling_gp": round(
                    float(ceiling["total_gross_profit"]) - float(official["total_gross_profit"]),
                    2,
                ),
                "official_gap_vs_position_first": round(
                    float(official["weighted_competitor_gap"]) - float(position_first["weighted_competitor_gap"]),
                    4,
                ),
            },
        }

    def _counterfactual_evidence(self, result: CounterfactualResult, *, plan: PlanBundle) -> dict[str, Any]:
        return {
            "intent": "OVERRIDE_WHAT_IF",
            "source_run_id": result.source_run_id,
            "what_if_run_id": result.result.run_id,
            "cached": result.cached,
            "result_status": result.result.status,
            "result_summary": result.result.summary,
            "official_summary": plan.official.summary,
            "comparison": result.comparison,
            "infeasibility": result.comparison.get("infeasibility"),
        }

    def _rule_what_if_evidence(self, *, plan: PlanBundle, intent: dict[str, Any]) -> dict[str, Any]:
        rule_name = intent.get("rule_name")
        rule_value = intent.get("rule_value")
        try:
            if rule_name == "budget_pct" and rule_value is not None:
                counterfactual = self.planner.simulate_counterfactual(plan.official.run_id, budget_pct=rule_value)
                return {**self._counterfactual_evidence(counterfactual, plan=plan), "rule_name": rule_name, "rule_value": rule_value}
            if rule_name == "min_margin_pct" and rule_value is not None and intent.get("upc"):
                counterfactual = self.planner.simulate_counterfactual(
                    plan.official.run_id,
                    min_margin_overrides={intent["upc"]: rule_value},
                )
                return {**self._counterfactual_evidence(counterfactual, plan=plan), "rule_name": rule_name, "rule_value": rule_value}
            if rule_name == "competitor_tolerance_pct" and rule_value is not None and intent.get("upc"):
                counterfactual = self.planner.simulate_counterfactual(
                    plan.official.run_id,
                    competitor_tolerance_overrides={intent["upc"]: rule_value},
                )
                return {**self._counterfactual_evidence(counterfactual, plan=plan), "rule_name": rule_name, "rule_value": rule_value}
        except ValueError as exc:
            return {
                "intent": "CLARIFY",
                "message": str(exc),
                "missing_fields": ["rule_value"],
                "starter_questions": self._starter_questions(plan),
            }
        return {
            "intent": "CLARIFY",
            "message": "Supported rule what-if inputs are budget, minimum margin by product, and competitor tolerance by product.",
            "missing_fields": ["rule_value"],
            "starter_questions": self._starter_questions(plan),
        }

    def _build_presentation(
        self,
        *,
        question: str,
        intent: dict[str, Any],
        evidence: dict[str, Any],
        plan: PlanBundle,
    ) -> dict[str, Any]:
        fallback = self._fallback_presentation(intent=intent, evidence=evidence, plan=plan)
        if not self.llm_client.configured:
            return fallback
        system_prompt = """
You are a pricing decision assistant for a bounded pricing-optimization demo.

Return JSON only with this schema:
{
  "headline": "short title",
  "summary": "2-4 sentence direct answer",
  "key_points": ["point 1", "point 2"],
  "caveat": "one short caution or boundary",
  "suggested_questions": ["question 1", "question 2"]
}

Rules:
1. Use only the evidence JSON.
2. Do not invent numbers, causes, or rules.
3. Keep the answer business-friendly and self-explanatory for a user new to this project.
4. Define unfamiliar terms briefly in plain language when needed.
5. If this is a what-if, say the official proposal is unchanged.
6. If the question is unsupported or incomplete, explain the supported scope instead of improvising.
7. Keep suggested questions inside the supported assistant scope.
""".strip()
        user_prompt = (
            f"Planner question:\n{question}\n\n"
            f"Detected intent:\n{json.dumps(intent, indent=2)}\n\n"
            f"Draft answer scaffold:\n{json.dumps(fallback, indent=2)}\n\n"
            f"Evidence JSON:\n{json.dumps(evidence, indent=2)}"
        )
        try:
            candidate = self.llm_client.chat_json(system_prompt=system_prompt, user_prompt=user_prompt)
            return self._validate_presentation(candidate, fallback=fallback)
        except Exception:
            return fallback

    def _validate_presentation(self, candidate: dict[str, Any], *, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            headline = str(candidate.get("headline", fallback["headline"])).strip()
            summary = str(candidate.get("summary", fallback["summary"])).strip()
            key_points = [str(item).strip() for item in candidate.get("key_points", fallback["key_points"]) if str(item).strip()]
            suggested = [
                str(item).strip()
                for item in candidate.get("suggested_questions", fallback["suggested_questions"])
                if str(item).strip()
            ]
            caveat = str(candidate.get("caveat", fallback["caveat"])).strip()
        except Exception:
            return fallback
        if not headline or not summary:
            return fallback
        if not self._contains_only_allowed_numbers(summary, fallback["summary"]):
            return fallback
        if not self._contains_only_allowed_numbers(caveat, fallback["caveat"]):
            return fallback
        return {
            "headline": headline,
            "summary": summary,
            "key_points": fallback["key_points"],
            "caveat": caveat or fallback["caveat"],
            "suggested_questions": suggested[:3] or fallback["suggested_questions"],
        }

    def _fallback_intent(self, question: str, plan: PlanBundle) -> dict[str, Any]:
        lowered = question.lower()
        resolved_upc = self._resolve_upc_reference(plan, question)
        pct_value = self._coerce_discount(self._extract_percent(question))

        if any(token in lowered for token in ["summary", "overview", "proposal", "plan", "recommendation"]):
            return self._intent_payload("PLAN_SUMMARY")
        if "help" in lowered or "support" in lowered or "what can you do" in lowered:
            return self._intent_payload("HELP")
        if "budget" in lowered and pct_value is not None:
            return self._intent_payload("RULE_WHAT_IF", rule_name="budget_pct", rule_value=pct_value)
        if "margin" in lowered and resolved_upc and pct_value is not None:
            return self._intent_payload(
                "RULE_WHAT_IF",
                upc=resolved_upc,
                rule_name="min_margin_pct",
                rule_value=pct_value,
            )
        if "competitor" in lowered and resolved_upc and pct_value is not None:
            return self._intent_payload(
                "RULE_WHAT_IF",
                upc=resolved_upc,
                rule_name="competitor_tolerance_pct",
                rule_value=pct_value,
            )
        if "why not" in lowered:
            if not resolved_upc:
                return self._intent_payload("CLARIFY", missing_fields=["upc"])
            if pct_value is None:
                return self._intent_payload("CLARIFY", upc=resolved_upc, missing_fields=["discount_pct"])
            return self._intent_payload("WHY_NOT", upc=resolved_upc, discount_pct=pct_value)
        if "what if" in lowered or "force" in lowered:
            if "budget" in lowered:
                if pct_value is None:
                    return self._intent_payload("CLARIFY", missing_fields=["rule_value"])
            if not resolved_upc:
                return self._intent_payload("CLARIFY", missing_fields=["upc"])
            if pct_value is None:
                return self._intent_payload("CLARIFY", upc=resolved_upc, missing_fields=["discount_pct"])
            return self._intent_payload("OVERRIDE_WHAT_IF", upc=resolved_upc, discount_pct=pct_value)
        if "why" in lowered:
            if not resolved_upc:
                return self._intent_payload("CLARIFY", missing_fields=["upc"])
            return self._intent_payload("WHY_SELECTED", upc=resolved_upc, discount_pct=pct_value)
        return self._intent_payload("UNSUPPORTED", confidence=0.2)

    def _intent_payload(
        self,
        intent: str,
        *,
        upc: str | None = None,
        discount_pct: float | None = None,
        rule_name: str | None = None,
        rule_value: float | None = None,
        confidence: float = 0.7,
        missing_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "intent": intent,
            "upc": upc,
            "discount_pct": discount_pct,
            "rule_name": rule_name,
            "rule_value": rule_value,
            "confidence": confidence,
            "rationale": "",
            "missing_fields": missing_fields or [],
            "scope": INTENT_SCOPE_TEXT[intent],
        }

    def _resolve_upc_reference(self, plan: PlanBundle, raw: str | None) -> str | None:
        if not raw:
            return None
        sku_match = re.search(r"\b(\d{4,})\b", raw)
        if sku_match:
            upc = sku_match.group(1)
            if upc in {row["upc"] for row in plan.catalog}:
                return upc
        normalized_raw = self._normalize_text(raw)
        product_map = {row["upc"]: row for row in plan.catalog}
        for upc, product in product_map.items():
            tokens = [
                product.get("upc", ""),
                product.get("product_label", ""),
                product.get("description", ""),
                product.get("category", ""),
                product.get("sub_category", ""),
            ]
            if any(self._normalize_text(token) and self._normalize_text(token) in normalized_raw for token in tokens):
                return upc
        for upc, product in product_map.items():
            label = self._normalize_text(product.get("product_label", ""))
            desc = self._normalize_text(product.get("description", ""))
            if normalized_raw and (normalized_raw in label or normalized_raw in desc):
                return upc
        return None

    def _normalize_text(self, value: str | None) -> str:
        if not value:
            return ""
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    def _extract_percent(self, question: str) -> float | None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", question)
        if not match:
            return None
        return float(match.group(1)) / 100

    def _coerce_discount(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return None

    def _simulate_locked_discount(self, plan: PlanBundle, upc: str, discount_pct: float) -> dict[str, Any] | None:
        selected = next((row for row in plan.official.selections if row["upc"] == upc), None)
        if selected is not None and abs(float(selected["discount_pct"]) - discount_pct) < 1e-9:
            return None
        result = self.planner.simulate_counterfactual(
            plan.official.run_id,
            exact_discount_locks={upc: discount_pct},
        )
        return self._counterfactual_evidence(result, plan=plan)

    def _starter_questions(self, plan: PlanBundle) -> list[str]:
        featured = sorted(
            plan.official.selections,
            key=lambda row: (-float(row["discount_pct"]), -float(row["gross_profit"])),
        )
        sample = featured[0] if featured else None
        if sample is None:
            return ["Summarize the proposal"]
        sample_alt = 0 if abs(float(sample["discount_pct"])) > 1e-9 else 0.05
        return [
            "Summarize the proposal",
            f"Why is {sample['product_label']} at {int(round(float(sample['discount_pct']) * 100))}%?",
            f"Why not {int(round(sample_alt * 100))}% for {sample['product_label']}?",
            f"What if we force {int(round(sample_alt * 100))}% for {sample['product_label']}?",
            "What if budget becomes 8%?",
        ]

    def _format_currency(self, value: float | int) -> str:
        amount = float(value)
        prefix = "-" if amount < 0 else ""
        return f"{prefix}${abs(amount):,.2f}"

    def _format_pct(self, value: float | int) -> str:
        return f"{float(value):.1%}"

    def _format_gap(self, value: float | int) -> str:
        return f"{float(value):,.4f}"

    def _format_count(self, value: float | int) -> str:
        return f"{int(round(float(value))):,}"

    def _fallback_presentation(self, *, intent: dict[str, Any], evidence: dict[str, Any], plan: PlanBundle) -> dict[str, Any]:
        name = intent["intent"]
        if name == "PLAN_SUMMARY":
            brief = evidence["plan_brief"]
            official = evidence["official"]
            tolerance_pct = float(brief.get("competitor_tradeoff_tolerance_pct", 0.0) or 0.0)
            return {
                "headline": "Recommended campaign summary",
                "summary": (
                    f"{brief['headline']} The plan promotes {self._format_count(official['promoted_products'])} of "
                    f"{self._format_count(official['selected_products'])} products, uses {self._format_pct(official['budget_utilization_pct'])} "
                    f"of the markdown budget, and keeps expected demand within on-hand inventory for every SKU."
                ),
                "key_points": [
                    f"Gross profit is {self._format_currency(official['total_gross_profit'])}, which is {self._format_currency(brief['profit_vs_current'])} versus current prices.",
                    f"Revenue changes by {self._format_currency(brief['revenue_vs_current'])} versus current prices.",
                    f"The competitor-aware solve can trade up to {self._format_pct(tolerance_pct)} of gross profit to reduce weighted competitor gap before using discount depth as the final tie-breaker.",
                    f"The price-position-first benchmark changes gross profit by {self._format_currency(brief['profit_vs_position_first'])} and competitor gap by {self._format_gap(brief['gap_improvement_vs_position_first'])} versus the official plan.",
                ],
                "caveat": "The official recommendation is the fixed proposal. What-if questions run separate comparison solves and never overwrite it.",
                "suggested_questions": self._starter_questions(plan)[:3],
            }
        if name == "WHY_SELECTED":
            dossier = evidence["sku_dossier"]
            selected = dossier["selected"]
            vs_current = dossier["selected_vs_current"]
            current_cf = evidence.get("current_counterfactual")
            local_best_cf = evidence.get("local_best_counterfactual")
            analysis = evidence.get("selection_analysis", {})
            primary_reason = analysis.get("primary_reason_label", "Portfolio fit")
            reason_detail = analysis.get("primary_reason_detail", "This point fits the official solve order best.")
            key_points = [
                f"Selected discount is {self._format_pct(selected['discount_pct'])} at {self._format_currency(selected['candidate_price'])}, with expected gross profit {self._format_currency(selected['gross_profit'])}.",
                f"Versus the current price point, this SKU changes gross profit by {self._format_currency(vs_current['gross_profit'])} and revenue by {self._format_currency(vs_current['revenue'])}.",
                f"Primary reason: {primary_reason}. {reason_detail}",
            ]
            if current_cf and current_cf["comparison"]["comparable"]:
                key_points.append(
                    f"If we force the current price for this product and re-solve the portfolio, total gross profit becomes {self._format_currency(current_cf['result_summary']['total_gross_profit'])}, a change of {self._format_currency(current_cf['comparison']['summary_delta']['total_gross_profit'])} versus the official plan."
                )
            elif local_best_cf and local_best_cf["comparison"]["comparable"]:
                key_points.append(
                    f"The SKU-local profit winner is feasible by itself, but a portfolio re-solve moves total gross profit by {self._format_currency(local_best_cf['comparison']['summary_delta']['total_gross_profit'])} and weighted competitor gap by {self._format_gap(local_best_cf['comparison']['summary_delta']['weighted_competitor_gap'])}."
                )
            return {
                "headline": f"Why this discount for {dossier['product']['product_label']}",
                "summary": (
                    f"The recommended campaign kept this product at the selected discount because it best fits the official portfolio solve order. In plain language: {reason_detail.lower()}"
                ),
                "key_points": key_points,
                "caveat": "This explains the fixed recommendation; it does not mean another discount is impossible.",
                "suggested_questions": [
                    f"Why not 0% for {dossier['product']['product_label']}?",
                    f"What if we force 0% for {dossier['product']['product_label']}?",
                    "What if budget becomes 8%?",
                ],
            }
        if name == "WHY_NOT":
            dossier = evidence["sku_dossier"]
            target = evidence.get("target_alternative")
            if target is None:
                return {
                    "headline": f"Need a valid discount for {dossier['product']['product_label']}",
                    "summary": "I could not map that request to one of the allowed discrete discount points for this product.",
                    "key_points": [
                        f"Supported discount buckets for this product are {', '.join(f'{int(round(x * 100))}%' for x in dossier['available_discount_buckets'])}.",
                    ],
                    "caveat": "This assistant only works with the discrete candidate discounts already prepared for the optimizer.",
                    "suggested_questions": self._starter_questions(plan)[:3],
                }
            if not target["effective_hard_valid"]:
                return {
                    "headline": f"Why {self._format_pct(target['discount_pct'])} was ruled out",
                    "summary": "That alternative was not selected because it fails at least one hard business rule before the portfolio solve even starts.",
                    "key_points": [
                        f"Blocking reason: {target['reason']}.",
                        f"At that point, expected gross margin is {self._format_pct(target['gross_margin_pct'])} and expected ending stock is {self._format_count(target['ending_inventory_units'])} units.",
                    ],
                    "caveat": "A hard-rule failure means the solver never treats this option as a valid candidate.",
                    "suggested_questions": [
                        f"What if we force {int(round(dossier['selected']['discount_pct'] * 100))}% for {dossier['product']['product_label']}?",
                        "What if budget becomes 8%?",
                    ],
                }
            alt_cf = evidence.get("alternative_counterfactual")
            key_points = [
                f"Requested alternative is {self._format_pct(target['discount_pct'])} with expected gross profit {self._format_currency(target['gross_profit'])}.",
                f"The selected point is {self._format_pct(dossier['selected']['discount_pct'])} with expected gross profit {self._format_currency(dossier['selected']['gross_profit'])}.",
            ]
            if alt_cf and alt_cf["comparison"]["comparable"]:
                key_points.append(
                    f"If we force {self._format_pct(target['discount_pct'])} and re-solve the portfolio, total gross profit becomes {self._format_currency(alt_cf['result_summary']['total_gross_profit'])}, changing by {self._format_currency(alt_cf['comparison']['summary_delta']['total_gross_profit'])}; weighted competitor gap changes by {self._format_gap(alt_cf['comparison']['summary_delta']['weighted_competitor_gap'])}."
                )
            return {
                "headline": f"Why not {self._format_pct(target['discount_pct'])} for {dossier['product']['product_label']}",
                "summary": "That alternative is feasible, but the full portfolio still preferred the selected point after applying the official solve order.",
                "key_points": key_points,
                "caveat": "Feasible does not always mean chosen; the portfolio objective sequence still decides the winner.",
                "suggested_questions": [
                    f"What if we force {int(round(target['discount_pct'] * 100))}% for {dossier['product']['product_label']}?",
                    "Summarize the proposal",
                ],
            }
        if name in {"OVERRIDE_WHAT_IF", "RULE_WHAT_IF"} and evidence.get("comparison"):
            comparison = evidence["comparison"]
            if comparison.get("comparable") is False:
                infeasibility = evidence.get("infeasibility") or {}
                conflict = (infeasibility.get("lock_conflicts") or [{}])[0]
                global_conflict = (infeasibility.get("global_conflicts") or [{}])[0]
                return {
                    "headline": "What-if result: infeasible",
                    "summary": "Official proposal unchanged. This what-if scenario cannot be solved under the current hard rules.",
                    "key_points": [
                        f"First blocker: {conflict.get('upc', 'n/a')} at {self._format_pct(conflict.get('discount_pct', 0.0))}." if conflict else "The requested lock conflicts with the current candidate set.",
                        f"Invalid SKU count under the new rules: {self._format_count(len(infeasibility.get('invalid_skus', [])))}.",
                        (
                            f"Portfolio-level blocker: minimum feasible markdown rate would be {self._format_pct(global_conflict.get('minimum_budget_utilization_pct', 0.0) or 0.0)} against a {self._format_pct(global_conflict.get('budget_limit_pct', 0.0) or 0.0)} limit."
                            if global_conflict and global_conflict.get("minimum_budget_utilization_pct") is not None
                            else "No portfolio-level blocker was detected before the solve."
                        ),
                    ],
                    "caveat": "Try another supported discount or relax one safe rule in a separate simulation.",
                    "suggested_questions": self._starter_questions(plan)[:3],
                }
            delta = comparison["summary_delta"]
            result_summary = evidence["result_summary"]
            return {
                "headline": "What-if result",
                "summary": "Official proposal unchanged. This answer comes from a separate child solve used only for comparison.",
                "key_points": [
                    f"Expected gross profit becomes {self._format_currency(result_summary['total_gross_profit'])}, a change of {self._format_currency(delta['total_gross_profit'])}.",
                    f"Revenue changes by {self._format_currency(delta['total_revenue'])} and markdown spend changes by {self._format_currency(delta['total_markdown_investment'])}.",
                    f"Competitor mismatch score changes by {self._format_gap(delta['weighted_competitor_gap'])}.",
                    f"{self._format_count(comparison['changed_sku_count'])} products change versus the official plan.",
                ],
                "caveat": "Counterfactual results help review the proposal, but they never overwrite the official recommendation.",
                "suggested_questions": self._starter_questions(plan)[:3],
            }
        if name == "HELP":
            return {
                "headline": "Supported question scope",
                "summary": "You can ask for the plan story, ask why one product got its discount, ask why another discrete discount was not chosen, or run one bounded what-if.",
                "key_points": self._starter_questions(plan),
                "caveat": "The assistant does not support open-ended optimization redesigns or free-form discount values outside the prepared candidate buckets.",
                "suggested_questions": self._starter_questions(plan)[:3],
            }
        if name == "CLARIFY":
            missing = evidence.get("missing_fields", [])
            readable = ", ".join(missing) if missing else "one missing field"
            return {
                "headline": "One detail is missing",
                "summary": f"I can answer this, but I still need {readable} to keep the request inside the supported scope.",
                "key_points": [
                    "Use a product name or UPC when asking about one specific product.",
                    "Use one of the discrete discount buckets such as 0%, 5%, 10%, 15%, 20%, or 25% when asking why not or what if.",
                ],
                "caveat": "The assistant stays intentionally narrow so every answer remains grounded in solver evidence.",
                "suggested_questions": self._starter_questions(plan)[:3],
            }
        return {
            "headline": "Supported scope only",
            "summary": evidence.get("message", "That question is outside the supported pricing assistant scope."),
            "key_points": self._starter_questions(plan),
            "caveat": "This demo supports only a small set of explanation and simulation scenarios.",
            "suggested_questions": self._starter_questions(plan)[:3],
        }

    def _build_selection_analysis(
        self,
        *,
        plan: PlanBundle,
        dossier: dict[str, Any],
        current_counterfactual: dict[str, Any] | None,
        local_best_counterfactual: dict[str, Any] | None,
    ) -> dict[str, Any]:
        selected = dossier["selected"]
        local_best = dossier["local_best_feasible"]
        invalid_alternative_count = sum(1 for row in dossier["alternatives"] if not row["effective_hard_valid"])
        if int(selected["candidate_rank"]) == int(local_best["candidate_rank"]):
            return {
                "primary_reason_code": "sku_local_best",
                "primary_reason_label": "Best feasible point for this SKU",
                "primary_reason_detail": "This discount is already the strongest gross-profit point that remains feasible for this SKU after margin and on-hand inventory screening.",
                "invalid_alternative_count": invalid_alternative_count,
            }

        if local_best_counterfactual and local_best_counterfactual["comparison"]["comparable"]:
            delta = local_best_counterfactual["comparison"]["summary_delta"]
            if float(delta["total_gross_profit"]) < -0.01:
                return {
                    "primary_reason_code": "portfolio_tradeoff",
                    "primary_reason_label": "Portfolio trade-off",
                    "primary_reason_detail": (
                        f"Forcing the SKU-local gross-profit winner would reduce portfolio gross profit by {self._format_currency(abs(delta['total_gross_profit']))} "
                        f"and rebalance {self._format_count(local_best_counterfactual['comparison']['changed_sku_count'])} SKUs."
                    ),
                    "invalid_alternative_count": invalid_alternative_count,
                }
            if float(delta["weighted_competitor_gap"]) > 0.0:
                return {
                    "primary_reason_code": "competitor_position",
                    "primary_reason_label": "Competitor position protection",
                    "primary_reason_detail": (
                        f"The selected point keeps the weighted competitor gap lower by {self._format_gap(delta['weighted_competitor_gap'])} while staying within the official profit tolerance."
                    ),
                    "invalid_alternative_count": invalid_alternative_count,
                }

        if current_counterfactual and current_counterfactual["comparison"]["comparable"]:
            delta = current_counterfactual["comparison"]["summary_delta"]
            if float(delta["total_gross_profit"]) < -0.01:
                return {
                    "primary_reason_code": "better_than_current",
                    "primary_reason_label": "Better than staying at current price",
                    "primary_reason_detail": (
                        f"Keeping the current price would lower portfolio gross profit by {self._format_currency(abs(delta['total_gross_profit']))}."
                    ),
                    "invalid_alternative_count": invalid_alternative_count,
                }

        return {
            "primary_reason_code": "portfolio_fit",
            "primary_reason_label": "Best portfolio fit",
            "primary_reason_detail": "This point fits the campaign-level profit, budget, and competitor-position trade-off best among the allowed discount buckets.",
            "invalid_alternative_count": invalid_alternative_count,
        }

    def _contains_only_allowed_numbers(self, candidate_text: str, fallback_text: str) -> bool:
        allowed = self._normalized_number_tokens(fallback_text)
        candidate = self._normalized_number_tokens(candidate_text)
        return candidate.issubset(allowed)

    def _normalized_number_tokens(self, text: str) -> set[str]:
        tokens = re.findall(r"[$]?-?\d[\d,]*(?:\.\d+)?%?|n/a", text.lower())
        normalized = set()
        for token in tokens:
            cleaned = token.replace("$", "").replace(",", "")
            normalized.add(cleaned)
        return normalized

    def _render_presentation(self, presentation: dict[str, Any]) -> str:
        lines = [f"**{presentation['headline']}**", "", presentation["summary"]]
        key_points = presentation.get("key_points") or []
        if key_points:
            lines.extend(["", "Key points"])
            lines.extend([f"- {point}" for point in key_points])
        caveat = presentation.get("caveat")
        if caveat:
            lines.extend(["", f"Caveat: {caveat}"])
        suggestions = presentation.get("suggested_questions") or []
        if suggestions:
            lines.extend(["", "Try next"])
            lines.extend([f"- {item}" for item in suggestions])
        return "\n".join(lines)
