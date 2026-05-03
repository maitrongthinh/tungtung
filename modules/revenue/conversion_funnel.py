from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from common.config import load_settings
from common.database import Database
from common.files import atomic_write_text, read_json
from common.logging import get_logger

logger = get_logger(__name__)


class ConversionFunnel:
    """Track and optimize the full conversion funnel: impressions -> clicks -> purchases."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.settings = load_settings()
        self.funnel_file = self.settings.memory_dir / "conversion_funnel.json"

    def load_funnel(self) -> dict[str, Any]:
        if self.funnel_file.exists():
            return read_json(self.funnel_file, default={})
        return {"daily": {}, "totals": {"impressions": 0, "clicks": 0, "purchases": 0, "revenue": 0}}

    def save_funnel(self, data: dict[str, Any]) -> None:
        atomic_write_text(self.funnel_file, json.dumps(data, ensure_ascii=False, indent=2))

    def record_impression(self, post_id: str) -> None:
        """Record that a post was shown to users (called when published)."""
        data = self.load_funnel()
        today = datetime.now(UTC).date().isoformat()
        if today not in data["daily"]:
            data["daily"][today] = {"impressions": 0, "clicks": 0, "purchases": 0, "revenue": 0, "posts": []}
        data["daily"][today]["impressions"] += 1
        data["daily"][today]["posts"].append(post_id)
        data["totals"]["impressions"] = sum(d.get("impressions", 0) for d in data["daily"].values())
        self.save_funnel(data)

    def record_click(self, post_id: str) -> None:
        """Record a click on an affiliate link."""
        data = self.load_funnel()
        today = datetime.now(UTC).date().isoformat()
        if today not in data["daily"]:
            data["daily"][today] = {"impressions": 0, "clicks": 0, "purchases": 0, "revenue": 0, "posts": []}
        data["daily"][today]["clicks"] += 1
        data["totals"]["clicks"] = sum(d.get("clicks", 0) for d in data["daily"].values())
        self.save_funnel(data)

    def record_purchase(self, post_id: str, amount: float) -> None:
        """Record a confirmed purchase from affiliate link."""
        data = self.load_funnel()
        today = datetime.now(UTC).date().isoformat()
        if today not in data["daily"]:
            data["daily"][today] = {"impressions": 0, "clicks": 0, "purchases": 0, "revenue": 0, "posts": []}
        data["daily"][today]["purchases"] += 1
        data["daily"][today]["revenue"] += amount
        data["totals"]["purchases"] = sum(d.get("purchases", 0) for d in data["daily"].values())
        data["totals"]["revenue"] = sum(d.get("revenue", 0) for d in data["daily"].values())
        self.save_funnel(data)

    def get_funnel_metrics(self) -> dict[str, Any]:
        """Get current funnel conversion rates."""
        data = self.load_funnel()
        totals = data.get("totals", {})
        impressions = totals.get("impressions", 0)
        clicks = totals.get("clicks", 0)
        purchases = totals.get("purchases", 0)
        revenue = totals.get("revenue", 0)
        return {
            "impressions": impressions,
            "clicks": clicks,
            "purchases": purchases,
            "revenue": revenue,
            "click_through_rate": round(clicks / max(impressions, 1) * 100, 2),
            "conversion_rate": round(purchases / max(clicks, 1) * 100, 2),
            "revenue_per_click": round(revenue / max(clicks, 1)),
            "revenue_per_impression": round(revenue / max(impressions, 1), 1),
            "daily_data": data.get("daily", {}),
        }

    def get_best_converting_posts(self, limit: int = 10) -> list[dict[str, Any]]:
        """Find posts with highest conversion rates for learning."""
        from common.models import PostFilters
        posts = self.database.list_posts(PostFilters(status="published", limit=200))
        converting = []
        for post in posts:
            if post.performance.clicks > 0:
                # Estimate conversion based on comment engagement
                engagement = post.performance.likes + post.performance.comments * 2 + post.performance.shares * 3
                conversion_score = engagement / max(post.performance.clicks, 1)
                converting.append({
                    "post_id": post.post_id,
                    "product": post.product.name[:50],
                    "category": post.product.category,
                    "clicks": post.performance.clicks,
                    "engagement": engagement,
                    "conversion_score": round(conversion_score, 2),
                    "content_preview": post.content.body[:100],
                    "hook": post.content.title[:60],
                })
        converting.sort(key=lambda x: x["conversion_score"], reverse=True)
        return converting[:limit]
