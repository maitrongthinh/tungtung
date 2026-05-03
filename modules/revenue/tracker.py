from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from common.config import load_settings
from common.database import Database
from common.files import atomic_write_text, read_json, write_json
from common.logging import get_logger

logger = get_logger(__name__)


class RevenueTracker:
    """Track affiliate revenue, conversion rates, and optimize strategy."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.settings = load_settings()
        self.revenue_file = self.settings.memory_dir / "revenue_data.json"

    def load_data(self) -> dict[str, Any]:
        if self.revenue_file.exists():
            return read_json(self.revenue_file, default={})
        return {"daily": {}, "totals": {"commission": 0, "orders": 0, "clicks": 0}}

    def save_data(self, data: dict[str, Any]) -> None:
        atomic_write_text(self.revenue_file, json.dumps(data, ensure_ascii=False, indent=2))

    def record_daily_metrics(self, day: str, metrics: dict[str, Any]) -> None:
        """Record daily revenue metrics."""
        data = self.load_data()
        data["daily"][day] = {
            "commission": metrics.get("commission", 0),
            "orders": metrics.get("orders", 0),
            "clicks": metrics.get("clicks", 0),
            "posts": metrics.get("posts", 0),
            "likes": metrics.get("likes", 0),
            "comments": metrics.get("comments", 0),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        # Update totals
        total_commission = sum(d.get("commission", 0) for d in data["daily"].values())
        total_orders = sum(d.get("orders", 0) for d in data["daily"].values())
        total_clicks = sum(d.get("clicks", 0) for d in data["daily"].values())
        data["totals"] = {"commission": total_commission, "orders": total_orders, "clicks": total_clicks}
        self.save_data(data)

    def get_best_posting_hours(self) -> list[int]:
        """Analyze activity log to find hours with most engagement."""
        events = self.database.get_activity_log(limit=1000, event_type="published")
        hour_counts: dict[int, int] = {}
        for event in events:
            try:
                ts = event.get("ts", "")
                hour = int(ts[11:13]) if len(ts) > 13 else 12
                hour_counts[hour] = hour_counts.get(hour, 0) + 1
            except (ValueError, IndexError):
                continue
        if not hour_counts:
            return [11, 12, 20, 21]  # Default peak hours
        sorted_hours = sorted(hour_counts.items(), key=lambda x: x[1], reverse=True)
        return [h for h, _ in sorted_hours[:4]]

    def get_top_categories(self, days: int = 7) -> list[dict[str, Any]]:
        """Get top performing categories by clicks from recent posts."""
        since = datetime.now(UTC) - timedelta(days=days)
        from common.models import PostFilters
        posts = self.database.list_posts(PostFilters(date_from=since, limit=500))
        category_stats: dict[str, dict] = {}
        for post in posts:
            cat = post.product.category
            if cat not in category_stats:
                category_stats[cat] = {"clicks": 0, "posts": 0, "likes": 0, "commission_rate": 0, "count": 0}
            stats = category_stats[cat]
            stats["clicks"] += post.performance.clicks
            stats["posts"] += 1
            stats["likes"] += post.performance.likes
            stats["commission_rate"] += post.product.commission_rate
            stats["count"] += 1
        result = []
        for cat, stats in category_stats.items():
            avg_commission = stats["commission_rate"] / max(stats["count"], 1)
            result.append({
                "category": cat,
                "clicks": stats["clicks"],
                "posts": stats["posts"],
                "likes": stats["likes"],
                "avg_commission_rate": round(avg_commission, 1),
                "clicks_per_post": round(stats["clicks"] / max(stats["posts"], 1), 1),
            })
        result.sort(key=lambda x: x["clicks"], reverse=True)
        return result

    def get_content_performance(self, days: int = 7) -> dict[str, Any]:
        """Analyze which content patterns perform best."""
        since = datetime.now(UTC) - timedelta(days=days)
        from common.models import PostFilters
        posts = self.database.list_posts(PostFilters(date_from=since, status="published", limit=200))
        if not posts:
            return {"total_posts": 0, "avg_clicks": 0, "best_hooks": [], "worst_hooks": []}
        sorted_posts = sorted(posts, key=lambda p: p.performance.clicks, reverse=True)
        total_clicks = sum(p.performance.clicks for p in posts)
        avg_clicks = total_clicks / max(len(posts), 1)
        best_hooks = [{"title": p.content.title[:60], "clicks": p.performance.clicks, "category": p.product.category} for p in sorted_posts[:5]]
        worst_hooks = [{"title": p.content.title[:60], "clicks": p.performance.clicks} for p in sorted_posts[-5:] if p.performance.clicks == 0]
        # Funnel analysis
        total_likes = sum(p.performance.likes for p in posts)
        total_comments = sum(p.performance.comments for p in posts)
        return {
            "total_posts": len(posts),
            "total_clicks": total_clicks,
            "avg_clicks_per_post": round(avg_clicks, 1),
            "total_likes": total_likes,
            "total_comments": total_comments,
            "click_through_rate": round(total_clicks / max(total_likes + total_comments, 1) * 100, 1),
            "best_hooks": best_hooks,
            "zero_click_posts": len(worst_hooks),
        }

    def get_roi_summary(self) -> dict[str, Any]:
        """Calculate ROI metrics."""
        data = self.load_data()
        kpi = self.database.get_daily_kpi(datetime.now(UTC))
        # Estimate: if avg commission is 5000 VND per order, and CTR is ~2%
        avg_commission = 5000  # VND per order estimate
        if data["totals"]["orders"] > 0 and data["totals"]["commission"] > 0:
            avg_commission = data["totals"]["commission"] / data["totals"]["orders"]
        est_revenue = kpi.get("clicks", 0) * 0.02 * avg_commission
        return {
            "today_clicks": kpi.get("clicks", 0),
            "today_posts": kpi.get("posts_published", 0),
            "today_likes": kpi.get("likes", 0),
            "today_comments": kpi.get("comments", 0),
            "total_tracked_commission": data["totals"]["commission"],
            "total_tracked_orders": data["totals"]["orders"],
            "avg_commission_per_order": round(avg_commission),
            "estimated_daily_revenue": round(est_revenue),
            "estimated_monthly_revenue": round(est_revenue * 30),
            "best_posting_hours": self.get_best_posting_hours(),
            "top_categories": self.get_top_categories()[:5],
        }
