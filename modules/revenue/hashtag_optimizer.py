from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from common.database import Database
from common.logging import get_logger

logger = get_logger(__name__)

# Base hashtags that always perform well in Vietnam Shopee context
_BASE_HASHTAGS = ["#shopee", "#dealhot", "#muasam", "#review", "#giamgia"]

# Seasonal hashtags by month
_SEASONAL_HASHTAGS: dict[int, list[str]] = {
    1: ["#tet", "#muasamtet", "#khuyenmaiTet"],
    2: ["#valentine", "#tinhnhan"],
    3: ["#8-3", "#phunu", "#quatenh"],
    6: ["#sale66", "#muahe"],
    9: ["#sale99", "#trungthu"],
    10: ["#sale1010"],
    11: ["#sale1111", "#blackfriday", "#ngaycuaMe"],
    12: ["#sale1212", "#noel", "#cuoinam"],
}


class HashtagOptimizer:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_trending_hashtags(self, limit: int = 10) -> list[str]:
        """Analyze published posts to find hashtags with most engagement."""
        since = datetime.now(UTC) - timedelta(days=14)
        from common.models import PostFilters
        posts = self.database.list_posts(PostFilters(date_from=since, status="published", limit=200))
        tag_engagement: Counter[str] = Counter()
        for post in posts:
            engagement = post.performance.clicks + post.performance.likes + post.performance.comments
            for tag in post.content.hashtags:
                tag_engagement[tag.lower()] += engagement
        # Merge with base + seasonal
        month = datetime.now().month
        seasonal = _SEASONAL_HASHTAGS.get(month, [])
        trending = [tag for tag, _ in tag_engagement.most_common(limit)]
        result = list(dict.fromkeys(_BASE_HASHTAGS + seasonal + trending))
        return result[:limit]

    def suggest_hashtags(self, category: str, limit: int = 8) -> list[str]:
        """Suggest optimal hashtags for a category."""
        trending = self.get_trending_hashtags(limit=limit)
        # Add category-specific tag
        cat_slug = re.sub(r"[^0-9a-zA-Z]+", "", category.replace(" ", ""))
        if cat_slug:
            cat_tag = f"#{cat_slug.lower()}"
            if cat_tag not in trending:
                trending.insert(0, cat_tag)
        return trending[:limit]
