from __future__ import annotations

import random
from typing import Any

from common.database import Database
from common.logging import get_logger
from common.models import PostRecord

logger = get_logger(__name__)

# Vietnamese engagement triggers that drive comments and shares
ENGAGEMENT_HOOKS = {
    "question": [
        "Cau hoi nay cho moi nguoi: ban da tung mua chua?",
        "Moi nguoi nghi sao? Comment xuong di minh cung ban luan",
        "Ai da dung roi cho minh xin review voi?",
        "Ban tinh mua mau gi? Comment minh tu van cho",
        "Moi nguoi vote di: Worth it hay khong?",
    ],
    "tag_friend": [
        "Tag ban be dang can cai nay di!",
        "Tag nguoi hay mua sam Shopee cung ban di",
        "Goi ten ban be hay san deal vao day",
        "Tag 3 nguoi can thay cai moi di",
    ],
    "urgency": [
        "Con 2h nua la het sale nha!",
        "Chi con 50 san pham trong kho thoi",
        "Flash sale ket thuc luc 0h hom nay",
        "Gia nay chi co hom nay thoi nha",
    ],
    "social_proof": [
        "{sold:,} nguoi da mua - ban da thu chua?",
        "4.8/5 sao tu {reviews:,} danh gia - khong lo",
        "Top 1 ban chay nhat category nay",
        "95% nguoi mua danh gia 5 sao",
    ],
    "curiosity": [
        "Minh tim duoc cai nay ma phai dung lai nhin 2 lan",
        "Deal nay ma khong chia se thi phi qua",
        "Cai nay ma giam gia nay thi khong tin duoc",
        "Hom nay Shopee co deal ma minh phai check lai 3 lan",
    ],
}


class EngagementBooster:
    """Optimize post content for maximum engagement and conversion."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def boost_cta(self, post: PostRecord) -> str:
        """Enhance CTA with engagement triggers."""
        original_cta = post.content.cta
        triggers = []
        # Add question trigger
        triggers.append(random.choice(ENGAGEMENT_HOOKS["question"]))
        # Add urgency if discount is high
        if post.product.discount_percent >= 30:
            triggers.append(random.choice(ENGAGEMENT_HOOKS["urgency"]))
        # Add social proof if available
        if post.product.sold_count > 100:
            template = random.choice(ENGAGEMENT_HOOKS["social_proof"])
            triggers.append(template.format(
                sold=post.product.sold_count,
                reviews=post.product.review_count,
            ))
        # Combine: original CTA + 1-2 triggers
        selected = random.sample(triggers, min(2, len(triggers)))
        return original_cta + "\n\n" + "\n".join(selected)

    def generate_poll_content(self, product: PostRecord) -> dict[str, str]:
        """Generate poll-style content to boost comments."""
        price_str = f"{product.price:,.0f}d"
        options = [
            f"A. Mua ngay vi giam {product.discount_percent:.0f}%",
            f"B. De lan sau co giam them",
            "C. Minh da mua roi, rat hai long",
            "D. Chua quan tam",
        ]
        body = f"Moi nguoi vote cho san pham nay di:\n\n{product.name}\nGia: {price_str}\n\n" + "\n".join(options)
        return {
            "title": f"Vote: Ban co mua {product.name[:40]} khong?",
            "body": body,
            "hashtags": ["#vote", "#muasam", "#shopee", "#review"],
        }

    def analyze_comment_intent(self, comment_text: str) -> dict[str, Any]:
        """Analyze comment to determine buying intent and suggested response."""
        text = comment_text.lower()
        high_intent = ["mua o dau", "link", "dat", "order", "bao nhieu", "gia bao", "con hang", "ship", "size", "mau"]
        medium_intent = ["review", "chat luong", "co tot", "dung thu", "dep khong", "xai co ben"]
        low_intent = ["dat qua", "khong mua", "mac", "binh thuong"]
        spam = ["spam", "quang cao", "lua dao", "fake", "gia"]

        if any(kw in text for kw in high_intent):
            return {"intent": "high", "action": "reply_with_link", "priority": 1, "suggested_reply": "link"}
        if any(kw in text for kw in medium_intent):
            return {"intent": "medium", "action": "reply_with_info", "priority": 2, "suggested_reply": "info"}
        if any(kw in text for kw in spam):
            return {"intent": "negative", "action": "reply_politely", "priority": 3, "suggested_reply": "defuse"}
        if any(kw in text for kw in low_intent):
            return {"intent": "low", "action": "optional_reply", "priority": 4, "suggested_reply": "encourage"}
        return {"intent": "neutral", "action": "like_or_skip", "priority": 5, "suggested_reply": "none"}
