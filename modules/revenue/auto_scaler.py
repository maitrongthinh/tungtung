from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.config import load_settings, save_runtime_config
from common.database import Database
from common.logging import get_logger

logger = get_logger(__name__)


class KPIAutoScaler:
    """Dynamically adjust posting KPIs based on performance data."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.settings = load_settings()

    def should_scale_up(self) -> dict[str, Any]:
        """Check if we should increase daily post target."""
        kpi = self.database.get_daily_kpi(datetime.now(UTC))
        clicks = kpi.get("clicks", 0)
        posts = kpi.get("posts_published", 0)
        current_target = self.settings.kpi.posts_per_day

        if posts == 0:
            return {"scale": False, "reason": "No posts today yet"}

        # Calculate click rate
        click_rate = clicks / max(posts, 1)

        # Scale up if getting good engagement
        if click_rate >= 3.0 and posts >= current_target * 0.8:
            new_target = min(current_target + 5, 35)  # Max 35 posts/day
            return {
                "scale": True,
                "direction": "up",
                "current": current_target,
                "suggested": new_target,
                "reason": f"High click rate ({click_rate:.1f}/post), scaling up",
                "click_rate": click_rate,
            }

        # Scale down if low engagement (save AI budget)
        if click_rate < 0.5 and posts >= current_target * 0.5:
            new_target = max(current_target - 3, 8)  # Min 8 posts/day
            return {
                "scale": True,
                "direction": "down",
                "current": current_target,
                "suggested": new_target,
                "reason": f"Low click rate ({click_rate:.1f}/post), scaling down to save budget",
                "click_rate": click_rate,
            }

        return {"scale": False, "reason": f"Click rate OK ({click_rate:.1f}/post)", "click_rate": click_rate}

    def get_optimal_settings(self) -> dict[str, Any]:
        """Suggest optimal settings based on data."""
        posts = self.database.list_recent_published_posts(hours=168, limit=500)  # 7 days
        if not posts:
            return {"suggestion": "Not enough data yet"}

        # Analyze by category performance
        category_perf: dict[str, dict] = {}
        for post in posts:
            cat = post.product.category
            if cat not in category_perf:
                category_perf[cat] = {"clicks": 0, "posts": 0, "likes": 0}
            category_perf[cat]["clicks"] += post.performance.clicks
            category_perf[cat]["posts"] += 1
            category_perf[cat]["likes"] += post.performance.likes

        # Find best and worst categories
        ranked = sorted(
            category_perf.items(),
            key=lambda x: x[1]["clicks"] / max(x[1]["posts"], 1),
            reverse=True,
        )

        best_cats = [cat for cat, _ in ranked[:3]]
        worst_cats = [cat for cat, _ in ranked[-3:] if _[1]["clicks"] == 0]

        total_clicks = sum(p.performance.clicks for p in posts)
        avg_clicks = total_clicks / max(len(posts), 1)

        suggestions = []
        if avg_clicks < 2:
            suggestions.append("Focus on high-commission products only")
        if len(worst_cats) > 2:
            suggestions.append(f"Drop low-performing categories: {', '.join(worst_cats)}")
        if best_cats:
            suggestions.append(f"Double down on: {', '.join(best_cats)}")

        return {
            "avg_clicks_per_post": round(avg_clicks, 1),
            "total_posts_7d": len(posts),
            "total_clicks_7d": total_clicks,
            "best_categories": best_cats,
            "drop_categories": worst_cats,
            "suggestions": suggestions,
            "scaling": self.should_scale_up(),
        }
