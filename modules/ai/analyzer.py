from __future__ import annotations

from statistics import mean
from typing import Any

from common.ai import cache_key, can_consume_ai_budget, estimate_tokens
from common.config import load_settings
from common.database import Database
from common.logging import get_logger
from common.models import ImprovementContext, ProductRecord
from modules.ai.client import JSONModelClient, OpenAIJSONClient

logger = get_logger(__name__)


class ProductAnalyzer:
    def __init__(self, database: Database | None = None, client: JSONModelClient | None = None) -> None:
        self.database = database
        self.client = client or OpenAIJSONClient()

    async def score_product(
        self,
        product: ProductRecord,
        *,
        category_average_price: float | None = None,
        improvement: ImprovementContext | None = None,
        memory_insights: list[str] | None = None,
        use_ai: bool = True,
    ) -> ProductRecord:
        base_score = self._heuristic_score(product, category_average_price, improvement)
        prompt_context = {
            "product_id": product.product_id,
            "price": product.price,
            "discount_percent": product.discount_percent,
            "sold_count": product.sold_count,
            "rating": product.rating,
            "category": product.category,
            "category_average_price": category_average_price,
            "watch_list_increase": improvement.watch_list_increase if improvement else [],
            "blacklist_keywords": improvement.blacklist_keywords if improvement else [],
            "memory_insights": memory_insights or [],
        }
        cache_lookup_key = cache_key("score", prompt_context)
        if self.database:
            cached = self.database.get_ai_cache(cache_lookup_key)
            if cached:
                return self._apply_payload(product, base_score, cached)
        if not use_ai:
            product.trend_score = round(base_score, 2)
            return product
        settings = load_settings(refresh=True)
        system_prompt = (
            "You evaluate ecommerce products for affiliate social content. "
            "Return strict JSON with score (0-100), reasons (list of strings), and risk_flags (list of strings)."
        )
        user_prompt = (
            f"Product: {product.model_dump_json()}\n"
            f"Category average price: {category_average_price}\n"
            f"Improvement context: {improvement.model_dump_json() if improvement else '{}'}\n"
            f"Long-term memory insights: {memory_insights or []}\n"
            f"Base heuristic score: {base_score}"
        )
        estimated_input = estimate_tokens(system_prompt, user_prompt)
        estimated_output = settings.ai.analyzer_max_tokens
        if self.database and not can_consume_ai_budget(
            self.database,
            settings.ai,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output,
        ):
            product.trend_score = round(base_score, 2)
            product.notes = [*product.notes, "AI scoring skipped due to daily budget limits"]
            return product
        try:
            payload = await self.client.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=settings.ai.analyzer_max_tokens,
                temperature=0.1,
            )
            if self.database:
                usage = getattr(self.client, "last_usage", None) or {}
                self.database.record_ai_usage(
                    purpose="score",
                    model=settings.ai.model,
                    input_tokens=int(usage.get("input_tokens") or estimated_input),
                    output_tokens=int(usage.get("output_tokens") or estimate_tokens(payload)),
                )
                self.database.set_ai_cache(
                    cache_key=cache_lookup_key,
                    kind="score",
                    payload=payload,
                    ttl_hours=settings.ai.scorer_cache_ttl_hours,
                )
            return self._apply_payload(product, base_score, payload)
        except Exception as exc:
            logger.warning("AI scoring fallback for %s: %s", product.product_id, exc)
            product.trend_score = round(base_score, 2)
            return product

    def _heuristic_score(
        self,
        product: ProductRecord,
        category_average_price: float | None,
        improvement: ImprovementContext | None,
    ) -> float:
        score = 0.0

        if product.price > 0:
            if category_average_price:
                ratio = category_average_price / product.price
                score += max(0.0, min(25.0, ratio * 12.0))
            else:
                score += 10.0

        score += min(20.0, product.sold_count / 500)
        score += min(20.0, (product.rating / 5.0) * 20.0)
        score += min(15.0, product.discount_percent * 0.5)
        score += 10.0 if product.image_path or product.images else 0.0
        score += min(10.0, product.review_count / 200)
        score += min(10.0, product.commission_rate * 2.0)

        if improvement:
            trend_hits = sum(1 for item in improvement.watch_list_increase if item.lower() in product.category.lower())
            blacklist_hits = sum(1 for item in improvement.blacklist_keywords if item.lower() in product.name.lower())
            score += trend_hits * 5.0
            score -= blacklist_hits * 8.0
            score += min(10.0, len(improvement.long_term_insights) * 1.5)

        return max(0.0, min(100.0, score))

    def category_average(self, products: list[ProductRecord]) -> float:
        prices = [product.price for product in products if product.price > 0]
        return mean(prices) if prices else 0.0

    def preview_score(
        self,
        product: ProductRecord,
        category_average_price: float | None = None,
        improvement: ImprovementContext | None = None,
    ) -> float:
        return self._heuristic_score(product, category_average_price, improvement)

    def _apply_payload(self, product: ProductRecord, base_score: float, payload: dict[str, Any]) -> ProductRecord:
        score = max(0.0, min(100.0, float(payload.get("score", base_score))))
        product.trend_score = round((base_score * 0.55) + (score * 0.45), 2)
        product.notes = [*product.notes, *payload.get("reasons", [])]
        return product
