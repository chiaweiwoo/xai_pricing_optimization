from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any
from urllib import error, request

from .db import json_dumps
from .planner import CounterfactualResult, PlanBundle, PricingDecisionService


SUPPORTED_INTENTS = {
    "PLAN_SUMMARY",
    "WHY_SELECTED",
    "WHY_NOT",
    "OVERRIDE_WHAT_IF",
    "RULE_WHAT_IF",
    "HELP",
    "UNSUPPORTED",
}


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    intent: dict[str, Any]
    response_text: str
    evidence: dict[str, Any]


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

    def chat_text(self, *, system_prompt: str, user_prompt: str) -> str:
        return self._request_chat(system_prompt=system_prompt, user_prompt=user_prompt, json_output=False)

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
        response_text = self._narrate(question=question, intent=intent, evidence=evidence)
        return ConversationTurn(
            question=question,
            intent=intent,
            response_text=response_text,
            evidence=evidence,
        )

    def _classify_intent(self, *, question: str, plan: PlanBundle) -> dict[str, Any]:
        fallback = self._fallback_intent(question, plan)
        if not self.llm_client.configured:
            return fallback

        prompt = f"""
Return JSON only.

Classify the planner question into one of:
- PLAN_SUMMARY
- WHY_SELECTED
- WHY_NOT
- OVERRIDE_WHAT_IF
- RULE_WHAT_IF
- HELP
- UNSUPPORTED

Supported rule what-if types:
- budget_pct
- safety_stock_pct
- min_margin_pct
- competitor_tolerance_pct

Supported override what-if type:
- exact SKU discount lock using existing discrete discounts only

Available scenario context:
- scenario_id: {plan.scenario_id}
- official_run_id: {plan.official.run_id}
- SKU ids: {", ".join(row["upc"] for row in plan.official.selections)}
- Allowed discount buckets seen in official plan data: {sorted({row["discount_pct"] for row in plan.official.selections})}

Return an object with keys:
intent, upc, discount_pct, rule_name, rule_value, confidence, rationale.
Use null for missing fields.
If the request is outside the supported set, return UNSUPPORTED.
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
        if upc is not None and upc not in {row["upc"] for row in plan.official.selections}:
            upc = None
        discount_pct = candidate.get("discount_pct")
        if discount_pct is not None:
            try:
                discount_pct = round(float(discount_pct), 4)
            except (TypeError, ValueError):
                discount_pct = None
        rule_name = candidate.get("rule_name")
        rule_value = candidate.get("rule_value")
        if rule_value is not None:
            try:
                rule_value = float(rule_value)
            except (TypeError, ValueError):
                rule_value = None
        validated = {
            "intent": intent_name,
            "upc": upc,
            "discount_pct": discount_pct,
            "rule_name": rule_name,
            "rule_value": rule_value,
            "confidence": candidate.get("confidence", fallback.get("confidence", 0.0)),
            "rationale": candidate.get("rationale", ""),
        }
        if intent_name in {"WHY_SELECTED", "WHY_NOT", "OVERRIDE_WHAT_IF"} and upc is None:
            return fallback
        if intent_name == "OVERRIDE_WHAT_IF" and discount_pct is None:
            return fallback
        if intent_name == "RULE_WHAT_IF" and rule_name is None:
            return fallback
        return validated

    def _build_evidence(self, *, plan: PlanBundle, intent: dict[str, Any], question: str) -> dict[str, Any]:
        name = intent["intent"]
        if name == "PLAN_SUMMARY":
            return self._plan_summary_evidence(plan)
        if name == "WHY_SELECTED":
            dossier = self.planner.get_sku_dossier(plan.official.run_id, intent["upc"])
            return {
                "intent": name,
                "question": question,
                "sku_dossier": dossier,
                "benchmark_comparison": self._plan_summary_evidence(plan)["benchmark_comparison"],
            }
        if name == "WHY_NOT":
            dossier = self.planner.get_sku_dossier(plan.official.run_id, intent["upc"])
            target = None
            if intent["discount_pct"] is not None:
                for alt in dossier["alternatives"]:
                    if abs(alt["discount_pct"] - intent["discount_pct"]) < 1e-9:
                        target = alt
                        break
            return {
                "intent": name,
                "question": question,
                "sku_dossier": dossier,
                "target_alternative": target,
            }
        if name == "OVERRIDE_WHAT_IF":
            counterfactual = self.planner.simulate_counterfactual(
                plan.official.run_id,
                exact_discount_locks={intent["upc"]: float(intent["discount_pct"] or 0.0)},
            )
            return self._counterfactual_evidence(counterfactual)
        if name == "RULE_WHAT_IF":
            return self._rule_what_if_evidence(plan=plan, intent=intent)
        if name == "HELP":
            return {
                "intent": "HELP",
                "supported_questions": [
                    "Summarize the plan",
                    "Why was this discount chosen for SKU 1001?",
                    "Why not 10% for SKU 1001?",
                    "What if we force 5% for SKU 1001?",
                    "What if budget becomes 8%?",
                    "What if minimum margin for SKU 1001 becomes 28%?",
                ],
            }
        return {
            "intent": "UNSUPPORTED",
            "message": "Supported questions are limited to plan summary, why/why not for a SKU, and a few safe what-if rules.",
        }

    def _plan_summary_evidence(self, plan: PlanBundle) -> dict[str, Any]:
        official = plan.official.summary
        profit_first = plan.profit_first.summary
        ceiling = plan.theoretical_ceiling.summary
        current = plan.current_price.summary
        return {
            "intent": "PLAN_SUMMARY",
            "official": official,
            "official_run_id": plan.official.run_id,
            "profit_first": profit_first,
            "current_price": current,
            "theoretical_ceiling": ceiling,
            "benchmark_comparison": {
                "vs_current_gp": round(float(official["total_gross_profit"]) - float(current["total_gross_profit"]), 2),
                "vs_profit_first_gp": round(
                    float(official["total_gross_profit"]) - float(profit_first["total_gross_profit"]),
                    2,
                ),
                "vs_ceiling_gp": round(
                    float(ceiling["total_gross_profit"]) - float(official["total_gross_profit"]),
                    2,
                ),
                "official_gap_vs_profit_first": round(
                    float(official["weighted_competitor_gap"]) - float(profit_first["weighted_competitor_gap"]),
                    4,
                ),
            },
        }

    def _counterfactual_evidence(self, result: CounterfactualResult) -> dict[str, Any]:
        return {
            "intent": "OVERRIDE_WHAT_IF",
            "source_run_id": result.source_run_id,
            "what_if_run_id": result.result.run_id,
            "cached": result.cached,
            "result_status": result.result.status,
            "result_summary": result.result.summary,
            "comparison": result.comparison,
        }

    def _rule_what_if_evidence(self, *, plan: PlanBundle, intent: dict[str, Any]) -> dict[str, Any]:
        rule_name = intent.get("rule_name")
        rule_value = intent.get("rule_value")
        if rule_name == "budget_pct" and rule_value is not None:
            counterfactual = self.planner.simulate_counterfactual(
                plan.official.run_id,
                budget_pct=rule_value,
            )
            return self._counterfactual_evidence(counterfactual)
        if rule_name == "safety_stock_pct" and rule_value is not None:
            counterfactual = self.planner.simulate_counterfactual(
                plan.official.run_id,
                safety_stock_pct=rule_value,
            )
            return self._counterfactual_evidence(counterfactual)
        if rule_name == "min_margin_pct" and rule_value is not None and intent.get("upc"):
            counterfactual = self.planner.simulate_counterfactual(
                plan.official.run_id,
                min_margin_overrides={intent["upc"]: rule_value},
            )
            return self._counterfactual_evidence(counterfactual)
        if rule_name == "competitor_tolerance_pct" and rule_value is not None and intent.get("upc"):
            counterfactual = self.planner.simulate_counterfactual(
                plan.official.run_id,
                competitor_tolerance_overrides={intent["upc"]: rule_value},
            )
            return self._counterfactual_evidence(counterfactual)
        return {
            "intent": "UNSUPPORTED",
            "message": "Supported rule what-if inputs are budget_pct, safety_stock_pct, min_margin_pct by SKU, and competitor_tolerance_pct by SKU.",
        }

    def _narrate(self, *, question: str, intent: dict[str, Any], evidence: dict[str, Any]) -> str:
        fallback = self._fallback_narration(intent=intent, evidence=evidence)
        if not self.llm_client.configured:
            return fallback
        system_prompt = """
You are a pricing decision assistant.
Use only the evidence JSON provided by the user.
Do not invent metrics, rules, or causal claims.
Keep the answer short, practical, and business-facing.
If the evidence is a what-if result, explicitly state that the official proposal is unchanged.
""".strip()
        user_prompt = f"Question: {question}\n\nEvidence JSON:\n{json.dumps(evidence, indent=2)}"
        try:
            return self.llm_client.chat_text(system_prompt=system_prompt, user_prompt=user_prompt).strip()
        except Exception:
            return fallback

    def _fallback_intent(self, question: str, plan: PlanBundle) -> dict[str, Any]:
        lowered = question.lower()
        sku_match = re.search(r"\b(\d{4,})\b", question)
        pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", question)
        upc = sku_match.group(1) if sku_match else None
        pct_value = round(float(pct_match.group(1)) / 100, 4) if pct_match else None

        if any(token in lowered for token in ["summary", "overview", "proposal", "plan"]):
            return {"intent": "PLAN_SUMMARY", "upc": None, "discount_pct": None, "rule_name": None, "rule_value": None, "confidence": 0.6}
        if "help" in lowered or "support" in lowered:
            return {"intent": "HELP", "upc": None, "discount_pct": None, "rule_name": None, "rule_value": None, "confidence": 0.7}
        if "why not" in lowered and upc:
            return {"intent": "WHY_NOT", "upc": upc, "discount_pct": pct_value, "rule_name": None, "rule_value": None, "confidence": 0.7}
        if "why" in lowered and upc:
            return {"intent": "WHY_SELECTED", "upc": upc, "discount_pct": pct_value, "rule_name": None, "rule_value": None, "confidence": 0.65}
        if "what if" in lowered and upc and pct_value is not None:
            return {"intent": "OVERRIDE_WHAT_IF", "upc": upc, "discount_pct": pct_value, "rule_name": None, "rule_value": None, "confidence": 0.7}
        if "budget" in lowered and pct_value is not None:
            return {"intent": "RULE_WHAT_IF", "upc": None, "discount_pct": None, "rule_name": "budget_pct", "rule_value": pct_value, "confidence": 0.7}
        if "safety stock" in lowered and pct_value is not None:
            return {"intent": "RULE_WHAT_IF", "upc": None, "discount_pct": None, "rule_name": "safety_stock_pct", "rule_value": pct_value, "confidence": 0.7}
        if "margin" in lowered and upc and pct_value is not None:
            return {"intent": "RULE_WHAT_IF", "upc": upc, "discount_pct": None, "rule_name": "min_margin_pct", "rule_value": pct_value, "confidence": 0.7}
        if "competitor" in lowered and upc and pct_value is not None:
            return {"intent": "RULE_WHAT_IF", "upc": upc, "discount_pct": None, "rule_name": "competitor_tolerance_pct", "rule_value": pct_value, "confidence": 0.7}

        # Gentle fallback if a SKU is present but wording is fuzzy.
        if upc in {row["upc"] for row in plan.official.selections}:
            return {"intent": "WHY_SELECTED", "upc": upc, "discount_pct": pct_value, "rule_name": None, "rule_value": None, "confidence": 0.4}
        return {"intent": "UNSUPPORTED", "upc": None, "discount_pct": None, "rule_name": None, "rule_value": None, "confidence": 0.2}

    def _fallback_narration(self, *, intent: dict[str, Any], evidence: dict[str, Any]) -> str:
        name = intent["intent"]
        if name == "PLAN_SUMMARY":
            official = evidence["official"]
            comparison = evidence["benchmark_comparison"]
            return (
                f"Official proposal uses {official['budget_utilization_pct']:.2%} of the 10% markdown budget, "
                f"promotes {official['promoted_products']} SKUs, and delivers gross profit of "
                f"{official['total_gross_profit']:.2f}. Versus the current-price baseline it adds "
                f"{comparison['vs_current_gp']:.2f} gross profit. Versus the profit-first feasible plan it gives up "
                f"{abs(comparison['vs_profit_first_gp']):.2f} gross profit in exchange for a tighter competitor position."
            )
        if name == "WHY_SELECTED":
            dossier = evidence["sku_dossier"]
            selected = dossier["selected"]
            current = dossier["current"]
            local_best = dossier["local_best_feasible"]
            return (
                f"SKU {dossier['upc']} is set to {selected['discount_pct']:.0%} because that candidate keeps the plan "
                f"feasible while landing expected gross profit of {selected['gross_profit']:.2f}. "
                f"The current price candidate would deliver {current['gross_profit']:.2f}, and the SKU-local best feasible "
                f"candidate would deliver {local_best['gross_profit']:.2f}. The portfolio still prefers the selected point "
                f"because the official run prioritizes competitor position before gross profit."
            )
        if name == "WHY_NOT":
            dossier = evidence["sku_dossier"]
            target = evidence.get("target_alternative")
            if target is None:
                return f"I could not map that alternative discount for SKU {dossier['upc']} to an allowed candidate."
            if not target["effective_hard_valid"]:
                return (
                    f"SKU {dossier['upc']} was not set to {target['discount_pct']:.0%} because that candidate is invalid "
                    f"under the current rules. The blocking reason is {target['reason']}."
                )
            selected = dossier["selected"]
            return (
                f"SKU {dossier['upc']} was not set to {target['discount_pct']:.0%}. That alternative is feasible, but the "
                f"official plan selected {selected['discount_pct']:.0%}. The alternative would deliver gross profit of "
                f"{target['gross_profit']:.2f} versus {selected['gross_profit']:.2f} for the selected point."
            )
        if name in {"OVERRIDE_WHAT_IF", "RULE_WHAT_IF"} and evidence.get("comparison"):
            delta = evidence["comparison"]["summary_delta"]
            return (
                f"This what-if re-solve leaves the official proposal unchanged. The simulated plan changes "
                f"{evidence['comparison']['changed_sku_count']} SKUs and moves gross profit by "
                f"{delta['total_gross_profit']:.2f}, revenue by {delta['total_revenue']:.2f}, and weighted competitor gap by "
                f"{delta['weighted_competitor_gap']:.4f}."
            )
        if name == "HELP":
            return "Ask for a plan summary, why a SKU got its discount, why not another discount, or a simple what-if override or rule change."
        return evidence.get("message", "That question is outside the supported pricing assistant scope.")
