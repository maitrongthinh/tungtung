from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Literal

from common.ai import cache_key, can_consume_ai_budget, estimate_tokens
from common.config import load_settings
from common.database import Database
from common.logging import get_logger
from common.models import AccountConfig, GeneratedContent
from modules.ai.client import JSONModelClient, OpenAIJSONClient

logger = get_logger(__name__)

FunPostType = Literal["meme", "tip"]

# Pool fallback nội dung meme (Gen Z vibe)
_MEME_TEMPLATES = [
    ("Nhà cháu đặt hàng Shopee xong ngồi F5 cả buổi 😂", "Ai mà không quen cái cảm giác này...\n\nĐặt đơn xong cứ 5 phút vào app check một lần 👀\n10 phút sau lại check tiếp.\n\nShipper đến mà mình không nghe chuông cửa là hỏng ngay 💀\n\nAi đồng cảnh ngộ không ơi?"),
    ("POV: Bạn bè nhờ mua đồ Shopee rồi... im lặng luôn 😅", "Kiểu:\n- 'Ơi mày order hộ tao cái này với'\n- 'Ok'\n- [2 ngày sau hàng về]\n- ...\n- 'Ủa hàng về chưa mày?'\n\nMình không phải dịch vụ vận chuyển nha mọi người 😭\n\nAi hay bị nhờ vậy không? Comment xuống mình cùng khóc nhau 👇"),
    ("Deal Shopee ngon mà không ai biết thì phí quá 🤌", "Hôm nay lướt Shopee thấy mấy cái deal mà mình phải dừng lại nhìn 2 lần 👁️👁️\n\nKhông biết tụi nó đặt giá kiểu gì nhưng rẻ thật.\n\nAi đang cần mua gì không? Drop xuống mình check giá cho 🔍"),
    ("Shopee Flash Sale lúc 12 giờ đêm là ai set up cái bẫy này 😤", "Ai cũng biết là không nên thức khuya.\nAi cũng biết là không nên mua sắm lúc nửa đêm.\n\nVà rồi...\n\n🛒 x3 items added to cart\n💳 Order placed successfully\n\nGoodnight 😴\n\nTag người hay bị trap flash sale nha 👇"),
]

# Pool fallback nội dung tip mua sắm
_TIP_TEMPLATES = [
    ("5 tips mua Shopee không bị hố mà ít ai nói", "Mình hay mua Shopee và tích lũy được mấy cái tips nhỏ:\n\n✅ Luôn check review 1 sao trước — người mua thật thường viết ở đó\n✅ Sort by 'Bán nhiều nhất' để biết shop nào uy tín\n✅ Flash Sale không phải lúc nào cũng rẻ hơn — check giá lịch sử\n✅ Chat với shop hỏi size/màu trước khi đặt\n✅ Screenshot mô tả sản phẩm khi mua — hữu ích khi cần khiếu nại\n\nMọi người có tip gì hay không? Chia sẻ xuống đây nha 👇"),
    ("Cách so sánh giá trên Shopee mà không mất thời gian", "Trick nhỏ mình hay dùng khi mua hàng:\n\n🔍 Search tên sản phẩm + 'review' để đọc ý kiến thật\n📊 Dùng tab 'Tốt nhất' rồi sort theo sold count\n💰 Check xem giá có bao gồm ship không (nhiều shop ẩn phí)\n⏰ Đặt hàng thứ 5-6 để kịp freeship cuối tuần\n\nBạn có cách nào hay hơn không? Mình đang học hỏi thêm 😄"),
    ("Bí kíp săn voucher Shopee mà nhiều người bỏ qua", "Mình vừa tổng hợp cách lấy voucher Shopee mà ít ai biết:\n\n1️⃣ Shop voucher: vào trang shop → click 'Theo dõi' → nhận voucher chào mừng\n2️⃣ Game trong app: Shopee Farm/Shopee Shake mỗi ngày → xu đổi voucher\n3️⃣ Livestream: nhiều shop phát voucher exclusive trong stream\n4️⃣ Chat với shop: hỏi thẳng 'có voucher không?' — khoảng 30% shop có\n\nAi có trick gì khác không? 👇"),
]


class FunContentWriter:
    def __init__(self, database: Database | None = None, client: JSONModelClient | None = None) -> None:
        self.database = database
        self.client = client or OpenAIJSONClient()

    async def write_fun_post(
        self,
        account: AccountConfig,
        post_type: FunPostType,
        category_context: str = "",
        use_ai: bool = True,
    ) -> GeneratedContent:
        prompt_context = {
            "account_id": account.id,
            "niche": account.niche,
            "post_type": post_type,
            "category_context": category_context,
        }
        lookup_key = cache_key("fun_writer", prompt_context)

        if self.database:
            cached = self.database.get_ai_cache(lookup_key)
            if cached:
                return self._normalize(cached, post_type)

        if not use_ai:
            return self._fallback(account, post_type)

        settings = load_settings(refresh=True)
        system_prompt, user_prompt = self._build_prompts(account, post_type, category_context)
        estimated_input = estimate_tokens(system_prompt, user_prompt)
        estimated_output = 400

        if self.database and not can_consume_ai_budget(
            self.database,
            settings.ai,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output,
        ):
            return self._fallback(account, post_type)

        try:
            payload = await self.client.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=estimated_output,
                temperature=random.uniform(0.6, 0.85),
            )
            if self.database:
                usage = getattr(self.client, "last_usage", None) or {}
                self.database.record_ai_usage(
                    purpose="fun_write",
                    model=settings.ai.model,
                    input_tokens=int(usage.get("input_tokens") or estimated_input),
                    output_tokens=int(usage.get("output_tokens") or estimated_output),
                )
                self.database.set_ai_cache(
                    cache_key=lookup_key,
                    kind="fun_write",
                    payload=payload,
                    ttl_hours=6,
                )
            return self._normalize(payload, post_type)
        except Exception as exc:
            logger.warning("FunContentWriter AI fallback (%s): %s", post_type, exc)
            return self._fallback(account, post_type)

    def _build_prompts(self, account: AccountConfig, post_type: FunPostType, category_context: str) -> tuple[str, str]:
        niche = account.niche or "shopee"
        page = account.page_name or account.id

        if post_type == "meme":
            system = (
                "Bạn là người viết nội dung Facebook Gen Z, chuyên về meme mua sắm Shopee Việt Nam. "
                "Phong cách: vui vẻ, relatable, dùng emoji vừa phải, tiếng Việt tự nhiên. "
                "KHÔNG quảng cáo sản phẩm cụ thể, KHÔNG có affiliate link. "
                "Chỉ nội dung giải trí để tăng engagement tự nhiên. "
                "Trả về JSON: {title, body, hashtags (list 4-6), cta, post_type}"
            )
            user = (
                f"Trang Facebook: {page} (niche: {niche})\n"
                f"Category liên quan: {category_context}\n"
                "Viết 1 bài meme/relatable về chủ đề mua sắm Shopee. "
                "Hook bằng câu mở đầu gây cười hoặc gật đầu đồng cảm. "
                "Body 3-5 dòng ngắn. CTA là câu hỏi để người đọc comment."
            )
        else:  # tip
            system = (
                "Bạn là chuyên gia mua sắm Shopee chia sẻ tips thực tế cho người Việt. "
                "Phong cách: hữu ích, ngắn gọn, dựa trên kinh nghiệm thực tế, tiếng Việt. "
                "KHÔNG quảng cáo sản phẩm cụ thể. Chỉ chia sẻ kiến thức/mẹo. "
                "Có thể đề cập generic về voucher, cách tìm deal, so sánh giá. "
                "Trả về JSON: {title, body, hashtags (list 4-6), cta, post_type}"
            )
            user = (
                f"Trang Facebook: {page} (niche: {niche})\n"
                f"Category context: {category_context}\n"
                "Viết 1 bài tips mua sắm Shopee thực tế. "
                "Title dạng listicle hoặc câu hỏi gây tò mò. "
                "Body: 3-5 tips/bước cụ thể có thể áp dụng ngay. "
                "CTA: mời người đọc chia sẻ kinh nghiệm của họ."
            )
        return system, user

    def _normalize(self, payload: dict, post_type: str) -> GeneratedContent:
        hashtags = payload.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = [t for t in hashtags.split() if t.startswith("#")]
        if not hashtags:
            hashtags = self._default_hashtags(post_type)
        return GeneratedContent(
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            hashtags=hashtags[:8],
            cta=str(payload.get("cta") or "Mọi người nghĩ sao? Comment xuống nhé 👇"),
            image_path="",
            best_post_time=datetime.now(UTC).strftime("%H:%M"),
            target_account="",
        )

    def _fallback(self, account: AccountConfig, post_type: FunPostType) -> GeneratedContent:
        if post_type == "meme":
            title, body = random.choice(_MEME_TEMPLATES)
        else:
            title, body = random.choice(_TIP_TEMPLATES)
        return GeneratedContent(
            title=title,
            body=body,
            hashtags=self._default_hashtags(post_type),
            cta="Mọi người thấy sao? Tag bạn bè vào bình luận nhé 👇",
            image_path="",
            best_post_time=datetime.now(UTC).strftime("%H:%M"),
            target_account=account.id,
        )

    def _default_hashtags(self, post_type: str) -> list[str]:
        base = ["#shopee", "#muasam", "#deal", "#review"]
        if post_type == "meme":
            return base + ["#memevietnam", "#relatable"]
        return base + ["#tips", "#muasamthongminh"]
