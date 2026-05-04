"""MetaMonitor — comment monitoring and auto-reply.

Delegates comment fetching and replying to the appropriate driver
(Graph API, cookie_page, or cookie_profile) based on account.auth_mode.
"""
from __future__ import annotations

from datetime import UTC, datetime

from common.ai import can_consume_ai_budget, estimate_tokens
from common.config import load_settings
from common.database import Database
from common.logging import get_logger
from common.models import AccountConfig, CommentRecord, PostRecord
from modules.ai.client import JSONModelClient, OpenAIJSONClient
from modules.meta.drivers import get_driver_for_account

logger = get_logger(__name__)

FLAG_KEYWORDS = (
    "giá",
    "mua",
    "link",
    "ship",
    "bao nhiêu",
    "giá bao",
    "mua ở đâu",
    "order",
    "đặt",
    "sp này",
    "sản phẩm này",
    "còn hàng",
    "hết hàng",
    "size",
    "màu",
    "chất lượng",
    "có tốt",
    "lừa đảo",
    "fake",
    "hàng nhái",
)


class MetaMonitor:
    def __init__(self, database: Database | None = None, client: JSONModelClient | None = None) -> None:
        self.database = database
        self.client = client or OpenAIJSONClient()

    async def fetch_comments(self, account: AccountConfig, fb_post_id: str) -> list[CommentRecord]:
        """Fetch comments using the account's driver."""
        driver = get_driver_for_account(account)
        raw = await driver.fetch_comments(account, fb_post_id)

        comments: list[CommentRecord] = []
        for item in raw:
            message = item.get("message", "")
            created_raw = item.get("created_time", "")
            created_at = datetime.now(UTC)
            if created_raw:
                try:
                    created_at = datetime.fromisoformat(
                        created_raw.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass

            comments.append(
                CommentRecord(
                    id=str(item.get("id", "")),
                    author=item.get("from", {}).get("name", "") if isinstance(item.get("from"), dict) else "",
                    message=message,
                    created_at=created_at,
                    flagged=any(keyword in message.lower() for keyword in FLAG_KEYWORDS),
                )
            )
        return comments

    async def monitor_posts(
        self, posts: list[PostRecord], accounts: dict[str, AccountConfig]
    ) -> dict[str, list[CommentRecord]]:
        results: dict[str, list[CommentRecord]] = {}
        for post in posts:
            if not post.fb_post_id:
                continue
            account = accounts.get(post.account)
            if not account:
                continue
            try:
                comments = await self.fetch_comments(account, post.fb_post_id)
                results[post.post_id] = comments
                settings = load_settings(refresh=True)
                if account.auto_reply and settings.features.auto_reply_enabled:
                    await self._reply_to_flagged_comments(account, post, comments)
            except Exception as exc:
                logger.warning("Comment monitoring failed for %s: %s", post.post_id, exc)
        return results

    async def _reply_to_flagged_comments(
        self,
        account: AccountConfig,
        post: PostRecord,
        comments: list[CommentRecord],
    ) -> None:
        """Reply to flagged comments using the account's driver."""
        driver = get_driver_for_account(account)
        for comment in comments:
            if not comment.flagged:
                continue
            if self.database and self.database.has_replied_comment(comment.id):
                continue

            reply_text = await self._generate_ai_reply(comment, post)
            if not reply_text:
                reply_text = self._fallback_reply(post)

            try:
                success = await driver.reply_comment(account, comment.id, reply_text)
                if success:
                    if self.database:
                        self.database.mark_comment_replied(comment.id, post.post_id)
                    logger.info("Replied to comment %s on post %s", comment.id, post.post_id)
            except Exception as exc:
                logger.warning("Failed to reply to comment %s: %s", comment.id, exc)

    async def _generate_ai_reply(self, comment: CommentRecord, post: PostRecord) -> str:
        settings = load_settings(refresh=True)
        if not settings.ai.enabled:
            return ""
        if self.database and not can_consume_ai_budget(
            self.database,
            settings.ai,
            estimated_input_tokens=200,
            estimated_output_tokens=80,
        ):
            return ""

        product = post.product
        affiliate_link = post.content.affiliate_link or product.affiliate_link or ""
        system_prompt = (
            "Bạn là người quản lý fanpage bán hàng affiliate Shopee trên Facebook. "
            "Trả lời comment một cách tự nhiên, thân thiện, ngắn gọn (1-2 câu). "
            "Ngôn ngữ: tiếng Việt, không cứng nhắc, không quảng cáo lộ liễu. "
            "Nếu hỏi giá: đề cập giá + gợi ý xem link. "
            "Nếu hỏi link/mua ở đâu: cung cấp link affiliate. "
            "Nếu hỏi chất lượng/đánh giá: chia sẻ rating + sold count nếu có. "
            "Nếu comment tiêu cực/nghi ngờ hàng fake: trả lời lịch sự, đề nghị xem review. "
            "Trả về JSON: {reply: string}"
        )
        price_str = f"{product.price:,.0f}đ" if product.price > 0 else "xem link"
        user_prompt = (
            f"Sản phẩm: {product.name[:80]}\n"
            f"Giá: {price_str} (giảm {product.discount_percent:.0f}%)\n"
            f"Rating: {product.rating:.1f}/5 | Đã bán: {product.sold_count:,}\n"
            f"Link mua: {affiliate_link}\n\n"
            f"Comment của người dùng ({comment.author or 'ẩn danh'}): {comment.message}"
        )

        try:
            payload = await self.client.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=120,
                temperature=0.45,
            )
            reply = str(payload.get("reply") or "").strip()
            if self.database:
                usage = getattr(self.client, "last_usage", None) or {}
                self.database.record_ai_usage(
                    purpose="reply",
                    model=settings.ai.model,
                    input_tokens=int(usage.get("input_tokens") or estimate_tokens(system_prompt, user_prompt)),
                    output_tokens=int(usage.get("output_tokens") or len(reply) // 4),
                )
            return reply
        except Exception as exc:
            logger.warning("AI reply generation failed: %s", exc)
            return ""

    def _fallback_reply(self, post: PostRecord) -> str:
        affiliate_link = post.content.affiliate_link or post.product.affiliate_link or ""
        if affiliate_link:
            return f"Bạn xem chi tiết và đặt hàng tại link trong bài nha 🛒 Nếu cần hỗ trợ thêm cứ nhắn tin cho mình!"
        return "Mình đã để thông tin trong bài rồi nha, bạn xem thử và nhắn tin nếu cần tư vấn thêm 😊"
