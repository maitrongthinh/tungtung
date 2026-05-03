from __future__ import annotations

import random
import re
from datetime import UTC, datetime
from typing import Iterable

from common.ai import cache_key, can_consume_ai_budget, estimate_tokens
from common.config import load_settings
from common.database import Database
from common.logging import get_logger
from common.models import AccountConfig, GeneratedContent, ImprovementContext, ProductRecord
from modules.ai.client import JSONModelClient, OpenAIJSONClient

logger = get_logger(__name__)


class ContentWriter:
    def __init__(self, database: Database | None = None, client: JSONModelClient | None = None) -> None:
        self.database = database
        self.client = client or OpenAIJSONClient()

    async def write_post(
        self,
        product: ProductRecord,
        account: AccountConfig,
        improvement: ImprovementContext,
        recent_posts: Iterable[str],
        memory_insights: list[str] | None = None,
        use_ai: bool = True,
    ) -> GeneratedContent:
        recent_snippets = "\n---\n".join(list(recent_posts)[:10])
        prompt_context = {
            "product_id": product.product_id,
            "account_id": account.id,
            "price": product.price,
            "discount_percent": product.discount_percent,
            "category": product.category,
            "page_name": account.page_name,
            "tone": account.tone,
            "memory_insights": memory_insights or [],
        }
        cache_lookup_key = cache_key("writer", prompt_context)
        if self.database:
            cached = self.database.get_ai_cache(cache_lookup_key)
            if cached:
                return self._normalize_payload(cached, product, account)
        if not use_ai:
            return self._fallback_payload(product, account)
        settings = load_settings(refresh=True)
        system_prompt = (
            "Bạn là copywriter Facebook chuyên affiliate Shopee cho thị trường Việt Nam. "
            "Nhiệm vụ: viết 1 bài đăng Facebook hoàn chỉnh, tự nhiên, không có mùi quảng cáo.\n\n"
            "QUY TẮC QUAN TRỌNG:\n"
            "1. PHÂN TÍCH TARGET AUDIENCE từ category + tên sản phẩm TRƯỚC, rồi chọn tone phù hợp:\n"
            "   - Anime/gaming/earphone/figure → Gen Z: dùng ngôn ngữ trendy, hài hước nhẹ, emoji vừa phải, có thể dùng tiếng lóng như 'ngon', 'xịn xò', 'flex'\n"
            "   - Đồ gia dụng/nhà bếp/mẹ & bé → Nội trợ/phụ nữ: nhẹ nhàng, thực tế, đánh vào nỗi lo tiết kiệm, tiện lợi cho gia đình\n"
            "   - Phụ kiện thời trang/mỹ phẩm → Chị em: cảm xúc, 'upgrade bản thân', kết hợp deal\n"
            "   - Đồ công nghệ/văn phòng → Dân công sở: thực dụng, highlight tính năng + giá trị\n"
            "2. CẤU TRÚC BÀI (PHẢI ĐỦ 3 PHẦN để không bị cắt 'Xem thêm'):\n"
            "   - Hook (1-2 câu): câu mở đầu gây tò mò, bất ngờ, hoặc đặt câu hỏi\n"
            "   - Body (3-5 bullet points hoặc đoạn ngắn): thông tin sản phẩm + giá + deal\n"
            "   - CTA (1-2 câu): kêu gọi hành động + câu hỏi tương tác để tăng comment\n"
            "3. ĐỘ DÀI body: 120-180 từ (đủ để Facebook hiển thị toàn bộ không cắt ngắn)\n"
            "4. TUYỆT ĐỐI KHÔNG: 'Sản phẩm tuyệt vời', 'chất lượng cao', ngôn ngữ bán hàng cứng nhắc\n"
            "5. PHẢI đề cập: giá hiện tại, % giảm (nếu có), sold count hoặc rating nếu ấn tượng\n"
            "6. Trả về JSON với keys: target_audience (phân tích 1 câu), tone_chosen, title, body, hashtags (list 5-8), cta, best_post_time, target_account"
        )
        user_prompt = (
            f"Thông tin tài khoản: page_name={account.page_name}, niche={account.niche}, tone_hint={account.tone}\n"
            f"Sản phẩm: tên={product.name}, giá={product.price:,.0f}đ, giá gốc={product.original_price:,.0f}đ, "
            f"giảm={product.discount_percent:.0f}%, đã bán={product.sold_count:,}, rating={product.rating:.1f}/5\n"
            f"Category: {product.category}\n"
            f"Memory insights hữu ích: {memory_insights or []}\n"
            f"Bài đăng gần đây (TRÁNH lặp lại cấu trúc/từ ngữ): {recent_snippets[:500] if recent_snippets else 'chưa có'}\n"
            "Hãy phân tích target audience từ sản phẩm, chọn tone phù hợp, rồi viết bài theo cấu trúc 3 phần."
        )
        estimated_input = estimate_tokens(system_prompt, user_prompt)
        estimated_output = settings.ai.writer_max_tokens
        if self.database and not can_consume_ai_budget(
            self.database,
            settings.ai,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output,
        ):
            return self._fallback_payload(product, account)
        try:
            payload = await self.client.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=settings.ai.writer_max_tokens,
                temperature=round(random.uniform(0.35, 0.55), 2),
            )
            if self.database:
                usage = getattr(self.client, "last_usage", None) or {}
                self.database.record_ai_usage(
                    purpose="write",
                    model=settings.ai.model,
                    input_tokens=int(usage.get("input_tokens") or estimated_input),
                    output_tokens=int(usage.get("output_tokens") or estimate_tokens(payload)),
                )
                self.database.set_ai_cache(
                    cache_key=cache_lookup_key,
                    kind="write",
                    payload=payload,
                    ttl_hours=settings.ai.writer_cache_ttl_hours,
                )
            return self._normalize_payload(payload, product, account)
        except Exception as exc:
            logger.warning("AI writer fallback for %s: %s", product.product_id, exc)
            return self._fallback_payload(product, account)

    def _normalize_payload(
        self,
        payload: dict,
        product: ProductRecord,
        account: AccountConfig,
    ) -> GeneratedContent:
        hashtags = payload.get("hashtags") or self._fallback_hashtags(product, account)
        if isinstance(hashtags, str):
            hashtags = [token for token in hashtags.split() if token.startswith("#")]
        image_path = product.image_path or ""
        cta = payload.get("cta") or f"Xem link ở đây: {product.affiliate_link}"
        if product.affiliate_link and product.affiliate_link not in cta:
            cta = f"{cta}\n{product.affiliate_link}"
        return GeneratedContent(
            title=str(payload.get("title") or product.name[:70]),
            body=str(payload.get("body") or self._fallback_body(product, account)),
            hashtags=hashtags[:10],
            cta=cta,
            image_path=image_path,
            best_post_time=str(payload.get("best_post_time") or self._default_post_time()),
            target_account=str(payload.get("target_account") or account.id),
        )

    def _fallback_payload(self, product: ProductRecord, account: AccountConfig) -> GeneratedContent:
        return GeneratedContent(
            title=f"Deal hot {product.discount_percent:.0f}% cho {product.name[:50]}",
            body=self._fallback_body(product, account),
            hashtags=self._fallback_hashtags(product, account),
            cta=f"Mình để link ở đây cho ai cần tham khảo nha: {product.affiliate_link}",
            image_path=product.image_path or "",
            best_post_time=self._default_post_time(),
            target_account=account.id,
        )

    def _fallback_body(self, product: ProductRecord, account: AccountConfig) -> str:
        niche = account.niche.lower()
        name = product.name
        price_str = self._format_price(product.price)
        original_str = self._format_price(product.original_price)
        discount = product.discount_percent
        sold = product.sold_count
        rating = product.rating

        # Phân tích target audience từ category/niche để chọn tone fallback
        is_gen_z = any(kw in niche for kw in ["anime", "gaming", "game", "tai nghe", "figure", "manga", "otaku"])
        is_family = any(kw in niche for kw in ["gia dụng", "nhà bếp", "mẹ", "bé", "nội trợ", "gia đình"])

        if is_gen_z:
            hook = f"Cái này xịn xò lắm mọi người ơi 👀 — {name}."
            deal_line = f"Đang sale {discount:.0f}% còn {price_str} thôi (gốc {original_str})."
            social_proof = f"Đã có {sold:,} người chốt, rating {rating:.1f}/5 ⭐ — flex được lắm!"
            cta_question = "Ai đang dùng rồi review cho mình với? Hay mọi người muốn mình so sánh thêm options cùng giá? 👇"
        elif is_family:
            hook = f"Mẹo tiết kiệm cho gia đình: {name} đang giảm giá tốt trên Shopee."
            deal_line = f"Giá chỉ {price_str} (giảm {discount:.0f}% từ {original_str}), giao hàng nhanh."
            social_proof = f"Hơn {sold:,} gia đình đã mua, đánh giá {rating:.1f}/5 — dùng thực tế mới thấy tiện."
            cta_question = "Nhà mình đang dùng loại nào rồi? Chia sẻ kinh nghiệm để chị em tham khảo nha! 💬"
        else:
            hook = f"Deal hôm nay đáng chú ý: {name}."
            deal_line = f"Giá hiện tại {price_str}, giảm {discount:.0f}% so với gốc {original_str}."
            social_proof = f"Shop đã bán {sold:,} sản phẩm, điểm đánh giá {rating:.1f}/5 — khá ổn để tham khảo."
            cta_question = "Mọi người thấy sao? Deal này có worth không hay để mình lọc thêm options khác? 👇"

        lines = [hook, "", deal_line, social_proof, "", cta_question]
        return "\n".join(lines)

    def _fallback_hashtags(self, product: ProductRecord, account: AccountConfig) -> list[str]:
        raw_tokens = [product.category, account.niche, account.page_name, "dealhot", "shopee", "giamgia", "review"]
        hashtags: list[str] = []
        for token in raw_tokens:
            slug = re.sub(r"[^0-9a-zA-ZÀ-ỹ]+", "", token.replace(" ", ""))
            if not slug:
                continue
            hashtags.append(f"#{slug}")
        return hashtags[:7]

    def _default_post_time(self) -> str:
        now = datetime.now(UTC)
        return now.strftime("%H:%M")

    def _format_price(self, value: float) -> str:
        return f"{value:,.0f}đ"
